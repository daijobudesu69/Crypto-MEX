"""Backtest MEX against Hyperliquid perp history, for a coin universe chosen
by market cap rather than a fixed list.

Why this exists: the repo's own backtest (T0-T14, MEX_BACKTEST_SPEC.md) was run
against Binance perp data on four hand-picked symbols. This script answers a
different question -- does the same strategy (mex/strategy.py, unmodified,
same compute_features()/step() the live forward test runs) still hold up on a
DIFFERENT venue's price history, across a WIDER, criteria-driven universe
(top N market cap, BTC and stablecoins excluded, restricted to whatever
Hyperliquid actually lists as a perp)? It is a fresh backtest, not a replay of
the validated one, and its results do not retroactively validate or invalidate
T0-T14.

Universe resolution (two independent data sources, both queried live, not
hand-maintained):
  1. CoinGecko /coins/markets, order=market_cap_desc -- ranks the whole
     market. BTC and every coin CoinGecko's own "stablecoins" category lists
     are dropped before ranking, so "top N" means top N genuinely volatile,
     non-BTC assets.
  2. Hyperliquid /info {"type": "meta"} -- the "universe" array is the
     authoritative list of what can actually be backtested (and later traded)
     there. A coin can rank in the top N and simply not be listed.
The final set is the intersection, in market-cap order, capped at N. A coin
dropped by the intersection is reported, not silently skipped, so the sample
this run actually used is auditable from the console output alone.

Historical bars come from Hyperliquid's own candleSnapshot endpoint --
NOT the Binance mirror mex/datafeed.py uses for the live forward test. Two
consequences: (a) this is the first backtest in the repo run entirely on
Hyperliquid's own price history, so there is no tracking-error number to
quote yet, unlike the Binance-mirror-vs-perp figures in datafeed.py's
docstring; (b) Hyperliquid's own perp history starts at that market's launch
(mainnet, mid-2023), which is shorter than the Binance history MEX was
originally validated on -- fewer bars means a less conclusive sample,
reported plainly in the summary rather than papered over.

Network note: every network call in this file is behind a small, separately
mockable function (_coingecko_get, _hl_info, _hl_candles) so tests can run
offline. This was written and unit-tested with those functions mocked; the
live HTTP calls have not been exercised from this environment because its own
network policy blocks both api.coingecko.com and api.hyperliquid.xyz. Run it
somewhere without that restriction before trusting its output.

Usage:  python tools/backtest_hyperliquid.py [--top-n 30] [--interval 4h]
Output: backtest/hyperliquid_trades.csv   (one row per closed trade)
        backtest/hyperliquid_summary.csv (one row per symbol + one TOTAL row)
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402
import requests  # noqa: E402

from mex.config import load as load_config  # noqa: E402
from mex.strategy import compute_features, step, Position  # noqa: E402

COINGECKO = "https://api.coingecko.com/api/v3/coins/markets"
HL_INFO = "https://api.hyperliquid.xyz/info"
UA = {"User-Agent": "Crypto-MEX-hyperliquid-backtest/1.0"}

# BTC exposure excluded per the request, wrapped form included -- WBTC tracks
# BTC's price, not an independent asset, and including it would just be BTC
# twice under a different ticker.
EXCLUDE_SYMBOLS = {"BTC", "WBTC"}

# Hyperliquid's own perp market opened here; a request earlier than this for
# any coin returns empty, which the pagination loop already treats as
# "not listed yet" rather than an error, but starting here saves empty round
# trips for the vast majority of coins.
HISTORY_START = pd.Timestamp("2023-01-01T00:00:00Z")
# Wall-clock width per candleSnapshot request. Hyperliquid's own limit on
# candles-per-response is not documented anywhere queried for this file; 45
# days of 4H bars is 270 candles per request, comfortably under any plausible
# cap, at the cost of more round trips than a tighter limit would need.
CHUNK = pd.Timedelta("45D")
NO_RETRY_STATUS = frozenset({400, 401, 403, 404, 429})


class BacktestError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# network seams -- each one mocked in tests/test_backtest_hyperliquid.py
# --------------------------------------------------------------------------- #
def _coingecko_get(params: dict, retries: int = 3) -> list:
    last = None
    for i in range(retries):
        try:
            r = requests.get(COINGECKO, params=params, headers=UA, timeout=25)
            if r.status_code in NO_RETRY_STATUS:
                raise BacktestError(f"CoinGecko HTTP {r.status_code} (tidak diulang)")
            r.raise_for_status()
            return r.json()
        except BacktestError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(2.0 * (i + 1))
    raise BacktestError(f"CoinGecko gagal: {last}")


def _hl_info(payload: dict, retries: int = 3) -> object:
    last = None
    for i in range(retries):
        try:
            r = requests.post(HL_INFO, json=payload, headers=UA, timeout=25)
            if r.status_code in NO_RETRY_STATUS:
                raise BacktestError(f"Hyperliquid HTTP {r.status_code} (tidak diulang)")
            r.raise_for_status()
            return r.json()
        except BacktestError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(2.0 * (i + 1))
    raise BacktestError(f"Hyperliquid /info gagal: {last}")


def _hl_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> list:
    return _hl_info({"type": "candleSnapshot",
                     "req": {"coin": coin, "interval": interval,
                             "startTime": start_ms, "endTime": end_ms}})


# --------------------------------------------------------------------------- #
# universe resolution -- pure logic, network calls injected as arguments
# --------------------------------------------------------------------------- #
def stablecoin_ids(fetch=_coingecko_get) -> set:
    ids, page = set(), 1
    while True:
        rows = fetch({"vs_currency": "usd", "category": "stablecoins",
                      "per_page": 250, "page": page})
        if not rows:
            break
        ids.update(r["id"] for r in rows)
        if len(rows) < 250:
            break
        page += 1
    return ids


def top_market_cap_symbols(top_n: int, exclude_ids: set,
                            fetch=_coingecko_get) -> list[str]:
    """Top-N tickers by market cap, BTC/WBTC and CoinGecko-listed stablecoins
    dropped BEFORE ranking to N -- so the result is N genuinely eligible
    coins, not N-minus-however-many-got-excluded."""
    out, page = [], 1
    while len(out) < top_n:
        rows = fetch({"vs_currency": "usd", "order": "market_cap_desc",
                      "per_page": 100, "page": page})
        if not rows:
            break
        for r in rows:
            if r["id"] in exclude_ids or r["symbol"].upper() in EXCLUDE_SYMBOLS:
                continue
            out.append(r["symbol"].upper())
            if len(out) >= top_n:
                break
        page += 1
        if page > 5:      # 500 coins deep and still short -- something's wrong
            break
    return out


def hyperliquid_universe(fetch=_hl_info) -> set:
    meta = fetch({"type": "meta"})
    return {a["name"] for a in meta.get("universe", []) if not a.get("isDelisted")}


def resolve_symbols(top_n: int, fetch_cg=_coingecko_get, fetch_hl=_hl_info) -> tuple:
    """Returns (symbols_to_backtest, skipped) in market-cap order.

    skipped is every top-N coin Hyperliquid does not list -- reported, never
    silently dropped, so the sample this run used is auditable from the log.
    """
    stables = stablecoin_ids(fetch_cg)
    ranked = top_market_cap_symbols(top_n, stables, fetch_cg)
    listed = hyperliquid_universe(fetch_hl)
    keep = [s for s in ranked if s in listed]
    skipped = [s for s in ranked if s not in listed]
    return keep, skipped


# --------------------------------------------------------------------------- #
# historical bars
# --------------------------------------------------------------------------- #
def fetch_history(coin: str, interval: str, start=HISTORY_START,
                  end=None, fetch=_hl_candles) -> pd.DataFrame:
    """Full available history for one coin, paginated forward from `start`.

    A window before the coin's actual listing returns empty -- not an error,
    just "nothing here yet" -- so the loop keeps stepping forward regardless.
    """
    end = end or pd.Timestamp.now(tz="UTC")
    rows, cur = [], start
    while cur < end:
        chunk_end = min(cur + CHUNK, end)
        raw = fetch(coin, interval, int(cur.timestamp() * 1000),
                    int(chunk_end.timestamp() * 1000))
        rows.extend(raw)
        cur = chunk_end
        time.sleep(0.15)   # polite pacing, not a documented rate limit
    if not rows:
        raise BacktestError(f"{coin}: tidak ada bar sama sekali dari Hyperliquid")
    df = pd.DataFrame({
        "ts": pd.to_datetime([int(r["t"]) for r in rows], unit="ms", utc=True),
        "open": [float(r["o"]) for r in rows], "high": [float(r["h"]) for r in rows],
        "low": [float(r["l"]) for r in rows], "close": [float(r["c"]) for r in rows],
        "volume": [float(r["v"]) for r in rows],
    })
    return (df.drop_duplicates(subset="ts").sort_values("ts")
              .reset_index(drop=True))


# --------------------------------------------------------------------------- #
# run step() over the full history and collect closed trades
# --------------------------------------------------------------------------- #
def run_backtest(symbol: str, df: pd.DataFrame, p) -> list[dict]:
    f = compute_features(df, p)
    ts = pd.DatetimeIndex(df["ts"])
    pos, pending, trades = None, None, []
    for i in range(len(df)):
        pos, pending, events = step(f, ts, i, p, pos, pending)
        for ev in events:
            if ev["event"] != "EXIT":
                continue
            po, px = ev["pos"], ev["exit_price"]
            trades.append({
                "symbol": symbol, "side": "long" if po.side > 0 else "short",
                "signal_bar_utc": po.signal_bar, "entry_bar_utc": po.entry_bar,
                "exit_bar_utc": ev["bar"].isoformat(),
                "entry_price": round(po.entry_price, 6), "exit_price": round(px, 6),
                "r_usdt": round(po.r_usdt, 6), "callback_pct": round(po.callback_pct, 4),
                "bars_held": po.bars_held,
                "result_R": round((px - po.entry_price) * po.side / po.r_usdt, 4),
                "mae_pct": round(po.mae_pct, 4), "mfe_pct": round(po.mfe_pct, 4),
                "exit_reason": ev["reason"],
            })
    return trades


def summarize(symbol: str, trades: list[dict], bars: int) -> dict:
    if not trades:
        return {"symbol": symbol, "bars": bars, "trades": 0, "win_rate_pct": "",
                "expectancy_R": "", "sum_R": "", "max_drawdown_R": ""}
    rs = [t["result_R"] for t in trades]
    equity, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    wins = sum(1 for r in rs if r > 0)
    return {
        "symbol": symbol, "bars": bars, "trades": len(rs),
        "win_rate_pct": round(100 * wins / len(rs), 1),
        "expectancy_R": round(sum(rs) / len(rs), 4),
        "sum_R": round(sum(rs), 4), "max_drawdown_R": round(dd, 4),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top-n", type=int, default=30)
    ap.add_argument("--interval", default="4h")
    args = ap.parse_args()

    p = load_config()["params"]
    print(f"[universe] resolving top {args.top_n} market cap ex-BTC/stablecoin, "
          f"terdaftar di Hyperliquid...")
    symbols, skipped = resolve_symbols(args.top_n)
    print(f"[universe] {len(symbols)} simbol dipakai: {', '.join(symbols)}")
    if skipped:
        print(f"[universe] {len(skipped)} simbol top-{args.top_n} TIDAK ada di "
              f"Hyperliquid, dilewati: {', '.join(skipped)}")
    if not symbols:
        raise BacktestError("tidak ada simbol yang cocok -- cek koneksi/filter")

    os.makedirs("backtest", exist_ok=True)
    all_trades, summaries = [], []
    for sym in symbols:
        print(f"[fetch] {sym}: mengunduh histori {args.interval}...")
        try:
            df = fetch_history(sym, args.interval)
        except BacktestError as e:
            print(f"[fetch] {sym} GAGAL, dilewati: {e}")
            continue
        trades = run_backtest(sym, df, p)
        all_trades.extend(trades)
        summaries.append(summarize(sym, trades, len(df)))
        print(f"[run] {sym}: {len(df)} bar, {len(trades)} transaksi")

    pd.DataFrame(all_trades).to_csv("backtest/hyperliquid_trades.csv", index=False)
    total_bars = sum(s["bars"] for s in summaries)
    total_trades = sum(s["trades"] for s in summaries)
    all_rs = [t["result_R"] for t in all_trades]
    summaries.append({
        "symbol": "TOTAL", "bars": total_bars, "trades": total_trades,
        "win_rate_pct": (round(100 * sum(1 for r in all_rs if r > 0) / len(all_rs), 1)
                         if all_rs else ""),
        "expectancy_R": round(sum(all_rs) / len(all_rs), 4) if all_rs else "",
        "sum_R": round(sum(all_rs), 4) if all_rs else "", "max_drawdown_R": "",
    })
    pd.DataFrame(summaries).to_csv("backtest/hyperliquid_summary.csv", index=False)
    print(f"\n[selesai] {total_trades} transaksi di {len(symbols)} simbol -> "
          f"backtest/hyperliquid_trades.csv, backtest/hyperliquid_summary.csv")


if __name__ == "__main__":
    sys.exit(main())
