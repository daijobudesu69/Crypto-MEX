"""MEX backtest on real Hyperliquid perp candles, top-N coins by market cap.

    python tools/backtest_hyperliquid.py                 # top 30, fees, no funding
    python tools/backtest_hyperliquid.py --funding       # + real hourly funding
    python tools/backtest_hyperliquid.py --binance-dir "../Backtest MEX 2.0 Strategy/data/klines"

Universe: CoinGecko ranking by market cap, minus BTC (and BTC wrappers),
stablecoins, gold-backed tokens and wrapped/staked/bridged derivatives, keeping
only coins that have a live Hyperliquid perp. Coins are taken in rank order
until N are found; every skipped coin is written to universe.csv with a reason.

The trade loop is mex.strategy.step() itself -- the same function the live
forward test runs -- so this backtest cannot drift from the live rules.

Hyperliquid serves only the most recent 5000 candles per interval. At 4H that
is ~833 days; there is no way to page further back, and coins listed later
have less. The window is printed in the report for exactly that reason.
"""
import argparse
import glob
import io
import json
import os
import re
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import mex.compat  # noqa: F401,E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402

from mex import config  # noqa: E402
from mex.strategy import compute_features, step  # noqa: E402

HL_INFO = "https://api.hyperliquid.xyz/info"
CG = "https://api.coingecko.com/api/v3"
UA = {"User-Agent": "Crypto-MEX-backtest/1.0 (+github.com/daijobudesu69/Crypto-MEX)"}

INTERVAL = "4h"
BAR = pd.Timedelta(INTERVAL)
HL_MAX_CANDLES = 5000
MIN_BARS = 300

# Hyperliquid tier-0 taker fee. Entry is a market order at the bar open and the
# exit is a triggered stop, so both sides pay taker.
DEFAULT_FEE_PCT = 0.045
DEFAULT_SLIP_PCT = 0.01

# CoinGecko's stablecoin category does not catch everything that should be out.
STABLE_EXTRA = {"usdt", "usdc", "dai", "usde", "usds", "fdusd", "tusd", "pyusd", "usdd",
                "usd1", "rlusd", "usdtb", "susde", "susds", "bfusd", "usdf", "usdg",
                "gho", "frax", "lusd", "crvusd", "eurc", "eurs", "usd0", "buidl", "ustb",
                "usyc", "usdy", "ousg"}
# A Hyperliquid ticker and a CoinGecko ticker can be two different coins. The
# live price has to agree within this much or the pairing is rejected.
MAX_PRICE_DIFF = 0.10
# Earlier Hyperliquid tickers of the same coin. TON was delisted after the
# 2026-06-15 08:00 bar and relisted as GRAM from 2026-07-02 at the same price
# (1.81 -> 1.64, CoinGecko id the-open-network). The 17-day hole means the two
# are backtested as separate segments, never stitched into one series.
HL_ALIASES = {"GRAM": ["TON"]}
GOLD = {"xaut", "paxg", "kau", "xaum"}
BTC_LIKE = {"btc", "wbtc", "cbbtc", "lbtc", "tbtc", "solvbtc", "btcb", "ubtc", "fbtc",
            "enzobtc", "clbtc", "pumpbtc", "brbtc", "unibtc"}
DERIVATIVE_ID = re.compile(
    r"(wrapped|staked|bridged|liquid-staking|restaked|-bridged|binance-peg|"
    r"^weth$|^steth$|^wsteth$|^weeth$|^reth$|^cbeth$|^meth$|^ezeth$|^rseth$|"
    r"jito-staked|msol|jupsol|bnsol|^wbeth$|^stbtc$|^lseth$|^oseth$|^sweth$)")
DERIVATIVE_SYM = {"weth", "steth", "wsteth", "weeth", "reth", "cbeth", "meth", "ezeth",
                  "rseth", "wbeth", "jitosol", "msol", "jupsol", "bnsol", "bbsol",
                  "stsol", "lseth", "oseth", "sweth", "wbnb", "whype", "khype", "sthype",
                  "stx-staked", "susdai"}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _request(method, url, *, params=None, body=None, retries=6, timeout=30):
    last = None
    for i in range(retries):
        try:
            r = requests.request(method, url, params=params, json=body,
                                 headers=UA, timeout=timeout)
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 0) or 0) or 10.0 * (i + 1)
                print(f"    429 dari {url.split('/')[2]}, tunggu {wait:.0f}s")
                time.sleep(wait)
                continue
            if r.status_code in (400, 401, 403, 404, 451):
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            return r.json()
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2.0 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def hl(body):
    return _request("POST", HL_INFO, body=body)


def cg(path, **params):
    return _request("GET", f"{CG}{path}", params=params)


# --------------------------------------------------------------------------- #
# universe
# --------------------------------------------------------------------------- #
def hl_perp_map(meta: dict) -> dict:
    """{ticker as CoinGecko writes it: Hyperliquid coin name}, live perps only.

    Hyperliquid lists sub-cent coins per 1000 units with a 'k' prefix (kPEPE,
    kSHIB, kBONK); those are the same coin and map back to the plain ticker.
    """
    out = {}
    for u in meta.get("universe", []):
        if u.get("isDelisted"):
            continue
        name = u["name"]
        base = name[1:] if (name.startswith("k") and len(name) > 1
                            and name[1:].isupper()) else name
        out.setdefault(base.upper(), name)
    return out


def exclusion_reason(coin: dict, stable_ids: set) -> str | None:
    cid, sym = coin["id"].lower(), coin["symbol"].lower()
    name = coin.get("name", "").lower()
    if sym in BTC_LIKE or cid == "bitcoin":
        return "btc"
    if cid in stable_ids or sym in STABLE_EXTRA:
        return "stablecoin"
    if sym in GOLD:
        return "gold-backed"
    if sym in DERIVATIVE_SYM or DERIVATIVE_ID.search(cid) or \
            any(w in name for w in ("wrapped", "staked", "bridged")):
        return "wrapped/staked derivative"
    return None


def hl_price(mids: dict, hl_coin: str) -> float | None:
    px = mids.get(hl_coin)
    if px is None:
        return None
    return float(px) / (1000.0 if hl_coin.startswith("k") and hl_coin[1:].isupper() else 1.0)


def select_universe(markets: list, stable_ids: set, perps: dict, n: int,
                    mids: dict | None = None):
    chosen, rows, seen = [], [], set()
    for c in sorted(markets, key=lambda m: m.get("market_cap_rank") or 10**9):
        sym = c["symbol"].upper()
        row = {"rank": c.get("market_cap_rank"), "id": c["id"], "symbol": sym,
               "market_cap_usd": c.get("market_cap"), "hl_coin": None,
               "status": None}
        why = exclusion_reason(c, stable_ids)
        hl_px = hl_price(mids, perps[sym]) if (mids and sym in perps) else None
        cg_px = c.get("current_price")
        if why:
            row["status"] = f"skip: {why}"
        elif sym in seen:
            row["status"] = "skip: ticker sudah diambil koin lain yang lebih besar"
        elif sym not in perps:
            row["status"] = "skip: tidak ada perp di Hyperliquid"
        elif hl_px and cg_px and abs(hl_px / cg_px - 1) > MAX_PRICE_DIFF:
            row["status"] = (f"skip: ticker bentrok, harga HL {hl_px:.6g} vs "
                             f"CoinGecko {cg_px:.6g}")
        else:
            row["hl_coin"] = perps[sym]
            row["status"] = "selected"
            chosen.append(row)
            seen.add(sym)
        rows.append(row)
        if len(chosen) >= n:
            break
    return chosen, rows


# --------------------------------------------------------------------------- #
# candles
# --------------------------------------------------------------------------- #
def parse_candles(raw: list, now: pd.Timestamp) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(raw)
    out = pd.DataFrame({
        "ts": pd.to_datetime(df["t"].astype("int64"), unit="ms", utc=True),
        "open": df["o"].astype(float), "high": df["h"].astype(float),
        "low": df["l"].astype(float), "close": df["c"].astype(float),
        "volume": df["v"].astype(float),
    })
    out = out[out["ts"] + BAR <= now]   # a bar opening at t closes at t + BAR
    return (out.drop_duplicates("ts", keep="last")
               .sort_values("ts").reset_index(drop=True))


def fetch_candles(coin: str, cache_dir: str, refresh: bool) -> pd.DataFrame:
    path = os.path.join(cache_dir, f"{coin}_{INTERVAL}.csv.gz")
    if os.path.exists(path) and not refresh:
        df = pd.read_csv(path)
        df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
        return df
    now = pd.Timestamp.now(tz="UTC")
    end = int(now.timestamp() * 1000)
    start = end - int((HL_MAX_CANDLES + 10) * BAR.total_seconds() * 1000)
    raw = hl({"type": "candleSnapshot",
              "req": {"coin": coin, "interval": INTERVAL,
                      "startTime": start, "endTime": end}})
    df = parse_candles(raw, now)
    df.to_csv(path, index=False)
    return df


def gap_count(df: pd.DataFrame) -> int:
    if len(df) < 2:
        return 0
    full = pd.date_range(df["ts"].iloc[0], df["ts"].iloc[-1], freq=BAR)
    return len(full) - len(df)


# --------------------------------------------------------------------------- #
# funding
# --------------------------------------------------------------------------- #
def fetch_funding(coin: str, start: pd.Timestamp, end: pd.Timestamp,
                  cache_dir: str, refresh: bool) -> pd.Series:
    """Hourly funding rate indexed by time. Positive = longs pay shorts."""
    path = os.path.join(cache_dir, f"{coin}_funding.csv.gz")
    if os.path.exists(path) and not refresh:
        df = pd.read_csv(path)
        df["time"] = pd.to_datetime(df["time"], utc=True, format="ISO8601")
        return df.set_index("time")["rate"]
    rows, cursor = [], int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    while cursor < end_ms:
        batch = hl({"type": "fundingHistory", "coin": coin,
                    "startTime": cursor, "endTime": end_ms})
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1]["time"]) + 1
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.25)
    df = pd.DataFrame({"time": pd.to_datetime([int(r["time"]) for r in rows], unit="ms", utc=True),
                       "rate": [float(r["fundingRate"]) for r in rows]})
    df = df.drop_duplicates("time").sort_values("time")
    df.to_csv(path, index=False)
    return df.set_index("time")["rate"]


def funding_r(trade: dict, rates: pd.Series | None) -> float:
    """Funding paid over the holding period, in R. Negative = cost."""
    if rates is None or rates.empty:
        return 0.0
    a, b = pd.Timestamp(trade["entry_time"]), pd.Timestamp(trade["exit_time"])
    total = rates[(rates.index > a) & (rates.index <= b)].sum()
    return float(-trade["side"] * total * trade["entry_price"] / trade["r_price"])


# --------------------------------------------------------------------------- #
# backtest
# --------------------------------------------------------------------------- #
def run_backtest(df: pd.DataFrame, p, fee_pct: float, slip_pct: float,
                 symbol: str = "") -> tuple[list, dict | None]:
    """Replay mex.strategy.step() bar by bar. Returns (closed trades, open trade)."""
    df = df.reset_index(drop=True)
    f = compute_features(df, p)
    ts = pd.DatetimeIndex(df["ts"])
    pos, pending, trades = None, None, []
    cost_frac = (fee_pct + slip_pct) / 100.0
    for i in range(len(df)):
        pos, pending, events = step(f, ts, i, p, pos, pending)
        for ev in events:
            if ev["event"] != "EXIT":
                continue
            ps, px = ev["pos"], float(ev["exit_price"])
            gross = ps.side * (px - ps.entry_price) / ps.r_usdt
            cost = cost_frac * (ps.entry_price + px) / ps.r_usdt
            trades.append({
                "symbol": symbol, "side": ps.side,
                "direction": "long" if ps.side > 0 else "fade_short",
                "signal_time": ps.signal_bar, "entry_time": ps.entry_bar,
                "exit_time": ev["bar"].isoformat(),
                "entry_price": ps.entry_price, "exit_price": px,
                "r_price": ps.r_usdt, "callback_pct": ps.callback_pct,
                "bars_held": ps.bars_held, "mae_pct": ps.mae_pct, "mfe_pct": ps.mfe_pct,
                "gross_R": gross, "cost_R": -cost, "funding_R": 0.0,
                "net_R": gross - cost,
            })
    open_trade = None
    if pos is not None:
        last = float(df["close"].iloc[-1])
        open_trade = {"symbol": symbol, "side": pos.side, "entry_time": pos.entry_bar,
                      "entry_price": pos.entry_price,
                      "mark_R": pos.side * (last - pos.entry_price) / pos.r_usdt}
    return trades, open_trade


def stats(trades: list, risk_pct: float, col: str = "net_R") -> dict:
    r = np.array([t[col] for t in trades], float)
    n = len(r)
    if n == 0:
        return {"avg_trade_pct": np.nan, "return_1x_pct": 0.0, "max_dd_1x_pct": 0.0,
                "trades": 0, "win_rate": np.nan, "exp_R": np.nan, "total_R": 0.0,
                "profit_factor": np.nan, "max_dd_R": 0.0, "return_pct": 0.0,
                "max_dd_pct": 0.0, "t_stat": np.nan, "avg_bars_held": np.nan}
    wins, losses = r[r > 0], r[r <= 0]
    cum = np.cumsum(r)
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    eq = np.cumprod(1 + r * risk_pct / 100.0)
    eq_peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
    sd = r.std(ddof=1) if n > 1 else np.nan
    # The same trades in % of price: full capital per trade, 1x, no leverage.
    # Every R component scales by 1R/entry, so net % = net R x 1R / entry.
    if all("r_price" in t for t in trades):
        pct = np.array([t[col] * t["r_price"] / t["entry_price"] * 100 for t in trades])
        eq1 = np.cumprod(1 + pct / 100)
        eq1_peak = np.maximum.accumulate(np.concatenate([[1.0], eq1]))[1:]
        pct_stats = {"avg_trade_pct": float(pct.mean()),
                     "return_1x_pct": float((eq1[-1] - 1) * 100),
                     "max_dd_1x_pct": float(np.max(1 - eq1 / eq1_peak) * 100)}
    else:
        pct_stats = {}
    return {**pct_stats,
        "trades": n,
        "win_rate": float(len(wins) / n),
        "exp_R": float(r.mean()),
        "total_R": float(r.sum()),
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else np.inf,
        "max_dd_R": float(np.max(peak - cum)),
        "return_pct": float((eq[-1] - 1) * 100),
        "max_dd_pct": float(np.max(1 - eq / eq_peak) * 100),
        "t_stat": float(r.mean() / sd * np.sqrt(n)) if n > 1 and sd > 0 else np.nan,
        "avg_bars_held": float(np.mean([t["bars_held"] for t in trades])),
    }


def portfolio_curve(trades: list, risk_pct: float) -> pd.DataFrame:
    """All coins at once, each trade risking risk_pct of the STARTING capital.

    Additive, not compounded: with 30 coins open at the same time, compounding
    per trade would size a trade off equity that other open trades have not
    realised yet. Summing on exit time keeps it honest and simple.
    """
    if not trades:
        return pd.DataFrame(columns=["exit_time", "pnl_pct", "equity_pct", "dd_pct"])
    d = pd.DataFrame(trades)[["exit_time", "net_R"]]
    d["exit_time"] = pd.to_datetime(d["exit_time"], utc=True)
    d = d.groupby("exit_time", as_index=False)["net_R"].sum().sort_values("exit_time")
    d["pnl_pct"] = d["net_R"] * risk_pct
    d["equity_pct"] = d["pnl_pct"].cumsum()
    d["dd_pct"] = np.maximum.accumulate(np.maximum(d["equity_pct"], 0)) - d["equity_pct"]
    return d.drop(columns="net_R")


# --------------------------------------------------------------------------- #
# Binance comparison (optional, local archive)
# --------------------------------------------------------------------------- #
def load_binance_archive(root: str, symbol: str) -> pd.DataFrame | None:
    for folder in (f"{symbol}USDT", f"1000{symbol}USDT"):
        files = sorted(glob.glob(os.path.join(root, folder, f"{folder}-{INTERVAL}-*.zip")))
        if files:
            break
    else:
        return None
    parts = []
    for fp in files:
        with open(fp, "rb") as fh:
            parts.append(read_binance_zip(fh.read()))
    return binance_frame(parts)


def read_binance_zip(blob: bytes) -> pd.DataFrame:
    """One data.binance.vision kline zip. Newer files have a header row, older do not."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        raw = z.read(z.namelist()[0]).decode()
    header = 0 if raw[:1].isalpha() else None
    d = pd.read_csv(io.StringIO(raw), header=header).iloc[:, :6]
    d.columns = ["ot", "open", "high", "low", "close", "volume"]
    return d


def binance_frame(parts: list) -> pd.DataFrame:
    d = pd.concat(parts, ignore_index=True)
    out = pd.DataFrame({
        "ts": pd.to_datetime(d["ot"].astype("int64"), unit="ms", utc=True),
        **{c: d[c].astype(float) for c in ("open", "high", "low", "close", "volume")}})
    return out.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def compare_with_binance(hl_df, bn_df, p, fee, slip, symbol):
    lo = max(hl_df["ts"].iloc[0], bn_df["ts"].iloc[0])
    hi = min(hl_df["ts"].iloc[-1], bn_df["ts"].iloc[-1])
    a = hl_df[(hl_df["ts"] >= lo) & (hl_df["ts"] <= hi)].reset_index(drop=True)
    b = bn_df[(bn_df["ts"] >= lo) & (bn_df["ts"] <= hi)].reset_index(drop=True)
    if len(a) < MIN_BARS or len(b) < MIN_BARS:
        return None
    fa, fb = compute_features(a, p), compute_features(b, p)
    sig = lambda f, df: set(df["ts"][f["long_signal"]]) | {("S", t) for t in df["ts"][f["fade_signal"]]}  # noqa: E731
    sa, sb = sig(fa, a), sig(fb, b)
    ta, _ = run_backtest(a, p, fee, slip, symbol)
    tb, _ = run_backtest(b, p, fee, slip, symbol)
    xa, xb = stats(ta, p.risk_pct), stats(tb, p.risk_pct)
    return {"symbol": symbol, "start": lo.isoformat(), "end": hi.isoformat(),
            "signals_hl": len(sa), "signals_binance": len(sb),
            "signal_match_pct": 100.0 * len(sa & sb) / max(len(sb), 1),
            "trades_hl": xa["trades"], "trades_binance": xb["trades"],
            "exp_R_hl": xa["exp_R"], "exp_R_binance": xb["exp_R"],
            "total_R_hl": xa["total_R"], "total_R_binance": xb["total_R"]}


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def _r(x, nd=2):
    return "—" if x is None or not np.isfinite(x) else f"{x:+.{nd}f}"


def _pct(x):
    return "—" if x is None or not np.isfinite(x) else f"{x * 100:.1f}%"


def _pf(x):
    return "∞" if x == np.inf else ("—" if not np.isfinite(x) else f"{x:.2f}")


def write_report(out_dir, args, p, summary, all_trades, universe_rows, port, cmp_rows,
                 opens):
    s = pd.DataFrame(summary)
    tot = stats(all_trades, p.risk_pct)
    longs = stats([t for t in all_trades if t["side"] > 0], p.risk_pct)
    shorts = stats([t for t in all_trades if t["side"] < 0], p.risk_pct)
    gross = stats(all_trades, p.risk_pct, col="gross_R")
    pos = int((s["exp_R"] > 0).sum())
    L = []
    L.append(f"# MEX backtest — Hyperliquid perp, top {len(s)} market cap (tanpa BTC & stablecoin)\n")
    L.append(f"Dibuat {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC oleh "
             "`tools/backtest_hyperliquid.py`. Parameter = `config.yaml` (tidak diubah). "
             f"Timeframe {INTERVAL}. Biaya: fee {args.fee_pct}% + slippage "
             f"{args.slip_pct}% per sisi (taker Hyperliquid tier 0). Funding: "
             + ("**ikut dihitung** (funding per jam asli Hyperliquid)." if args.funding
                else "**tidak** dihitung.") + "\n")
    L.append("> **Batas data:** Hyperliquid hanya menyimpan 5.000 candle terakhir per "
             f"interval. Di 4H itu mulai **{s['start'].min()[:10]}** — lebih pendek dari "
             "backtest Binance asli (2023-06 → 2026-08). Koin yang listing belakangan "
             "punya data lebih sedikit lagi (lihat kolom `Bars`).\n")
    L.append("## Ringkasan gabungan (semua koin)\n")
    L.append("| | Semua | Long | Fade short | Patokan ETH Binance |")
    L.append("|---|---|---|---|---|")
    L.append(f"| Transaksi | {tot['trades']} | {longs['trades']} | {shorts['trades']} | 118 |")
    L.append(f"| Win rate | {_pct(tot['win_rate'])} | {_pct(longs['win_rate'])} | {_pct(shorts['win_rate'])} | 44.9% |")
    L.append(f"| Expectancy (net) | {_r(tot['exp_R'], 3)} R | {_r(longs['exp_R'], 3)} R | {_r(shorts['exp_R'], 3)} R | +0.31 R |")
    L.append(f"| Expectancy (sebelum biaya) | {_r(gross['exp_R'], 3)} R | | | |")
    if args.funding and all_trades:
        L.append(f"| — dimakan fee+slippage | {_r(np.mean([t['cost_R'] for t in all_trades]), 3)} R | | | |")
        L.append(f"| — dimakan funding | {_r(np.mean([t['funding_R'] for t in all_trades]), 3)} R | | | |")
    L.append(f"| Total R | {_r(tot['total_R'], 1)} | {_r(longs['total_R'], 1)} | {_r(shorts['total_R'], 1)} | |")
    L.append(f"| Profit factor | {_pf(tot['profit_factor'])} | {_pf(longs['profit_factor'])} | {_pf(shorts['profit_factor'])} | 1.95 |")
    L.append(f"| t-stat | {_r(tot['t_stat'])} | {_r(longs['t_stat'])} | {_r(shorts['t_stat'])} | 2.20 |")
    L.append(f"| Koin dengan expectancy > 0 | **{pos} / {len(s)}** | | | 18 / 21 |")
    L.append("")
    if len(port):
        eq = 100.0 + port["equity_pct"]
        rel_dd = float((1 - eq / np.maximum.accumulate(np.maximum(eq, 100.0))).max() * 100)
        L.append(f"**Portofolio** (semua {len(s)} koin jalan bersamaan, tiap transaksi "
                 f"risiko {p.risk_pct}% dari modal awal, tidak dimajemukkan): "
                 f"PnL **{port['equity_pct'].iloc[-1]:+.1f}%** dari modal awal. Max drawdown "
                 f"**{port['dd_pct'].max():.1f} poin** modal awal = **{rel_dd:.1f}%** dari "
                 "ekuitas puncak. Dengan 30 koin terbuka bersamaan, risiko 1% per transaksi "
                 "berarti eksposur total bisa jauh di atas 1% — koin-koin ini bergerak searah.\n")
    L.append("## Per koin — profit dalam %\n")
    L.append("Dua cara membaca % profit dari transaksi yang sama (semua sudah dipotong "
             "fee, slippage" + (" dan funding" if args.funding else "") + "):\n")
    L.append("- **Risiko 1%/trx** — ukuran posisi = 1% modal ÷ 1R, persis cara strategi "
             "ini dijalankan. Dimajemukkan.\n"
             "- **Modal penuh 1x** — seluruh modal masuk tiap transaksi, tanpa leverage. "
             "Ini sama dengan % gerak harga yang ditangkap. Dimajemukkan.\n")
    L.append("| # | Koin | Dari | Trx | Win | Rata2/trx | **Profit @risiko 1%** | Max DD @1% "
             "| **Profit modal penuh 1x** | Max DD 1x | Exp R | PF |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in s.sort_values("return_pct", ascending=False).itertuples():
        L.append(f"| {r.rank} | {r.symbol} | {r.start[:10]} | {r.trades} | {_pct(r.win_rate)} | "
                 f"{r.avg_trade_pct:+.2f}% | **{r.return_pct:+.1f}%** | {r.max_dd_pct:.1f}% | "
                 f"**{r.return_1x_pct:+.1f}%** | {r.max_dd_1x_pct:.1f}% | "
                 f"{_r(r.exp_R, 3)} | {_pf(r.profit_factor)} |")
    L.append("")
    if opens:
        L.append("Posisi yang masih terbuka di bar terakhir (tidak dihitung di atas): "
                 + ", ".join(f"{o['symbol']} {'L' if o['side'] > 0 else 'S'} "
                             f"{o['mark_R']:+.2f}R" for o in opens) + ".\n")
    if cmp_rows:
        c = pd.DataFrame(cmp_rows)
        L.append("## Hyperliquid vs Binance perp — jendela waktu yang sama\n")
        L.append("Mesin dan parameter sama; yang beda cuma sumber candle. "
                 "`Sinyal cocok` = persen sinyal Binance yang juga muncul di bar yang sama di Hyperliquid.\n")
        L.append("| Koin | Jendela | Sinyal cocok | Trx HL / BN | Exp R HL | Exp R BN | Total R HL | Total R BN |")
        L.append("|---|---|---|---|---|---|---|---|")
        for r in c.itertuples():
            L.append(f"| {r.symbol} | {r.start[:10]} → {r.end[:10]} | {r.signal_match_pct:.0f}% | "
                     f"{r.trades_hl} / {r.trades_binance} | {_r(r.exp_R_hl, 3)} | {_r(r.exp_R_binance, 3)} | "
                     f"{_r(r.total_R_hl, 1)} | {_r(r.total_R_binance, 1)} |")
        L.append(f"Rata-rata sinyal cocok: **{c['signal_match_pct'].mean():.0f}%**. "
                 f"Total R di koin-koin ini: Hyperliquid **{c['total_R_hl'].sum():+.1f}** vs "
                 f"Binance **{c['total_R_binance'].sum():+.1f}**.\n")
        L.append("")
    skipped = [u for u in universe_rows if u["status"] != "selected"]
    if skipped:
        L.append("## Koin yang dilewati (urut market cap)\n")
        for u in skipped:
            L.append(f"- #{u['rank']} {u['symbol']} (`{u['id']}`) — {u['status'][6:]}")
        L.append("")
    L.append("## File\n")
    L.append("- `summary.csv` — tabel per koin di atas\n- `trades.csv` — semua transaksi "
             "(gross/biaya/funding/net dalam R)\n- `portfolio_equity.csv` — kurva portofolio\n"
             "- `universe.csv` — seleksi koin + alasan tiap yang dilewati\n"
             "- `data/` — cache candle Hyperliquid mentah (hapus atau `--refresh` untuk unduh ulang)")
    with open(os.path.join(out_dir, "REPORT.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--fee-pct", type=float, default=DEFAULT_FEE_PCT)
    ap.add_argument("--slip-pct", type=float, default=DEFAULT_SLIP_PCT)
    ap.add_argument("--funding", action="store_true", help="hitung funding per jam asli")
    ap.add_argument("--binance-dir", help="folder arsip klines Binance perp untuk pembanding")
    ap.add_argument("--out", default=os.path.join(ROOT, "backtest", "hyperliquid"))
    ap.add_argument("--refresh", action="store_true", help="abaikan cache, unduh ulang")
    args = ap.parse_args(argv)

    p = config.load()["params"]
    data_dir = os.path.join(args.out, "data")
    os.makedirs(data_dir, exist_ok=True)

    print("[1] universe: CoinGecko market cap + perp Hyperliquid")
    perps = hl_perp_map(hl({"type": "meta"}))
    mids = hl({"type": "allMids"})
    markets = cg("/coins/markets", vs_currency="usd", order="market_cap_desc",
                 per_page=250, page=1)
    stables = cg("/coins/markets", vs_currency="usd", category="stablecoins",
                 order="market_cap_desc", per_page=250, page=1)
    stable_ids = {s["id"] for s in stables}
    chosen, universe_rows = select_universe(markets, stable_ids, perps, args.top, mids)
    pd.DataFrame(universe_rows).to_csv(os.path.join(args.out, "universe.csv"), index=False)
    print(f"    {len(chosen)} koin: " + ", ".join(c["symbol"] for c in chosen))

    summary, all_trades, opens, cmp_rows = [], [], [], []
    for k, c in enumerate(chosen, 1):
        coin = c["hl_coin"]
        segments = [(a, fetch_candles(a, data_dir, args.refresh))
                    for a in HL_ALIASES.get(coin, [])]
        segments.append((coin, fetch_candles(coin, data_dir, args.refresh)))
        segments = [(name, d) for name, d in segments if len(d) >= MIN_BARS]
        if not segments:
            print(f"[{k:2}/{len(chosen)}] {coin:8} kurang dari {MIN_BARS} bar, dilewati")
            continue
        trades, open_trade = [], None
        for name, d in segments:
            seg_trades, open_trade = run_backtest(d, p, args.fee_pct, args.slip_pct, c["symbol"])
            for t in seg_trades:
                t["hl_coin"] = name
            if args.funding and seg_trades:
                rates = fetch_funding(name, d["ts"].iloc[0], d["ts"].iloc[-1] + BAR,
                                      data_dir, args.refresh)
                for t in seg_trades:
                    t["funding_R"] = funding_r(t, rates)
                    t["net_R"] += t["funding_R"]
            trades += seg_trades
        df = segments[-1][1]
        st = stats(trades, p.risk_pct)
        summary.append({"rank": c["rank"], "symbol": c["symbol"],
                        "hl_coin": "+".join(n for n, _ in segments),
                        "bars": sum(len(d) for _, d in segments),
                        "gaps": sum(gap_count(d) for _, d in segments),
                        "start": segments[0][1]["ts"].iloc[0].isoformat(),
                        "end": df["ts"].iloc[-1].isoformat(),
                        "longs": sum(t["side"] > 0 for t in trades),
                        "shorts": sum(t["side"] < 0 for t in trades), **st})
        all_trades += trades
        if open_trade:
            opens.append(open_trade)
        if args.binance_dir:
            bn = load_binance_archive(args.binance_dir, c["symbol"])
            if bn is not None:
                row = compare_with_binance(df, bn, p, args.fee_pct, args.slip_pct, c["symbol"])
                if row:
                    cmp_rows.append(row)
        print(f"[{k:2}/{len(chosen)}] {summary[-1]['hl_coin']:8} {summary[-1]['bars']:5} bar  {st['trades']:3} trx  "
              f"exp {st['exp_R']:+.3f}R  total {st['total_R']:+6.1f}R")
        time.sleep(0.3)

    port = portfolio_curve(all_trades, p.risk_pct)
    pd.DataFrame(summary).to_csv(os.path.join(args.out, "summary.csv"), index=False)
    pd.DataFrame(all_trades).to_csv(os.path.join(args.out, "trades.csv"), index=False)
    port.to_csv(os.path.join(args.out, "portfolio_equity.csv"), index=False)
    if cmp_rows:
        pd.DataFrame(cmp_rows).to_csv(os.path.join(args.out, "compare_binance.csv"), index=False)
    with open(os.path.join(args.out, "run_meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"generated_utc": pd.Timestamp.now(tz="UTC").isoformat(),
                   "args": vars(args), "params": p.__dict__}, fh, indent=2, default=str)
    write_report(args.out, args, p, summary, all_trades, universe_rows, port, cmp_rows, opens)
    tot = stats(all_trades, p.risk_pct)
    print(f"\nGabungan: {tot['trades']} trx, exp {tot['exp_R']:+.3f}R, "
          f"win {tot['win_rate']:.1%}, PF {tot['profit_factor']:.2f}  ->  {args.out}")


if __name__ == "__main__":
    main()
