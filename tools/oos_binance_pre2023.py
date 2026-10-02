"""Uji B (docs/OOS_PLAN.md §4): MEX on Binance perp, Jan 2020 -> May 2023.

    python tools/oos_binance_pre2023.py            # downloads once, then uses the cache
    python tools/oos_binance_pre2023.py --refresh  # download again

Nothing before Jun 2023 was ever used to build, tune, validate or select this
strategy, so it is the only genuinely out-of-sample stretch of history. The
universe is the same 24 coins as Uji A; a coin without a Binance perp (or with
fewer than 300 bars) in the window is reported as missing, never replaced.

Source: the public monthly archive at data.binance.vision (USDT-M perp 4h klines
and 8h funding). fapi.binance.com is geo-blocked from here; the archive is not.
"""
import argparse
import io
import json
import os
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import mex.compat  # noqa: F401,E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402

import backtest_hyperliquid as bh  # noqa: E402
import oos_common as oc  # noqa: E402
from mex import config  # noqa: E402

VISION = "https://data.binance.vision/data/futures/um/monthly"
FEE_PCT = 0.05          # Binance USDT-M tier-0 taker, same as tools/h2h_hl_binance.py
SLIP_PCT = 0.01
WIN_START = pd.Timestamp("2020-01-01", tz="UTC")
WIN_END = pd.Timestamp("2023-06-01", tz="UTC")       # entries strictly before this
FIRST_MONTH, LAST_MONTH = "2020-01", "2023-06"        # June only closes late-May entries
MIN_BARS = 300


def binance_symbol(sym: str) -> str:
    return "1000SHIBUSDT" if sym == "SHIB" else f"{sym}USDT"


def months(first: str = FIRST_MONTH, last: str = LAST_MONTH) -> list:
    return [m.strftime("%Y-%m") for m in pd.period_range(first, last, freq="M")]


def read_funding_zip(blob: bytes) -> pd.DataFrame:
    """One fundingRate zip: calc_time (ms), funding_interval_hours, last_funding_rate."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        raw = z.read(z.namelist()[0]).decode()
    header = 0 if raw[:1].isalpha() else None
    d = pd.read_csv(io.StringIO(raw), header=header)
    d = d.iloc[:, [0, d.shape[1] - 1]]
    d.columns = ["calc_time", "rate"]
    return pd.DataFrame({"time": pd.to_datetime(d["calc_time"].astype("int64"), unit="ms", utc=True),
                         "rate": d["rate"].astype(float)})


def _get(session, url):
    r = session.get(url, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content


def download(bsym: str, cache_dir: str, refresh: bool):
    """(candles, funding series, months with candles, months with funding). Cached."""
    kp = os.path.join(cache_dir, f"{bsym}_4h.csv.gz")
    fp = os.path.join(cache_dir, f"{bsym}_funding.csv.gz")
    mp = os.path.join(cache_dir, f"{bsym}_months.json")
    if not refresh and all(os.path.exists(x) for x in (kp, fp, mp)):
        k = pd.read_csv(kp)
        k["ts"] = pd.to_datetime(k["ts"], utc=True, format="ISO8601")
        f = pd.read_csv(fp)
        f["time"] = pd.to_datetime(f["time"], utc=True, format="ISO8601")
        with open(mp, encoding="utf-8") as fh:
            m = json.load(fh)
        return k, f.set_index("time")["rate"], m["klines"], m["funding"]
    ms = months()
    with requests.Session() as s, ThreadPoolExecutor(8) as ex:
        s.headers.update(bh.UA)
        kb = list(ex.map(lambda m: _get(s, f"{VISION}/klines/{bsym}/4h/{bsym}-4h-{m}.zip"), ms))
        fb = list(ex.map(lambda m: _get(s, f"{VISION}/fundingRate/{bsym}/{bsym}-fundingRate-{m}.zip"), ms))
    k_months = [m for m, b in zip(ms, kb) if b]
    f_months = [m for m, b in zip(ms, fb) if b]
    k = (bh.binance_frame([bh.read_binance_zip(b) for b in kb if b]) if k_months
         else pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"]))
    f = (pd.concat([read_funding_zip(b) for b in fb if b]).drop_duplicates("time").sort_values("time")
         if f_months else pd.DataFrame(columns=["time", "rate"]))
    k.to_csv(kp, index=False)
    f.to_csv(fp, index=False)
    with open(mp, "w", encoding="utf-8") as fh:
        json.dump({"klines": k_months, "funding": f_months}, fh)
    f["time"] = pd.to_datetime(f["time"], utc=True)
    return k, f.set_index("time")["rate"], k_months, f_months


def run_coin(sym: str, df: pd.DataFrame, rates: pd.Series, p) -> tuple[list, int]:
    """Trades entered inside the window, net of costs + funding; and how many were still open."""
    trades, open_trade = bh.run_backtest(df, p, FEE_PCT, SLIP_PCT, sym)
    for t in trades:
        t["funding_R"] = bh.funding_r(t, rates)
        t["net_R"] += t["funding_R"]
    trades = oc.in_window(trades, WIN_START, WIN_END, closed=False)
    still_open = int(open_trade is not None and pd.Timestamp(open_trade["entry_time"]) < WIN_END)
    return trades, still_open


def analyse(trades: list, coins: pd.DataFrame, risk_pct: float, hl_exp: dict) -> dict:
    tested = coins[coins["status"] == "diuji"]
    pooled = bh.stats(trades, risk_pct)
    share = float((tested["exp_R"] > 0).mean()) if len(tested) else np.nan
    mt, n_months = oc.monthly_t(trades)
    L = [t for t in trades if t["symbol"] in oc.GROUP_L]
    O = [t for t in trades if t["symbol"] in oc.GROUP_O]
    diff, tw = oc.welch([t["net_R"] for t in L], [t["net_R"] for t in O])
    mdt, mdn = oc.monthly_diff_t(L, O)
    years = {}
    for t in trades:
        years.setdefault(pd.Timestamp(t["entry_time"]).year, []).append(t)
    return {
        "pooled": pooled, "share_positive": share, "n_positive": int((tested["exp_R"] > 0).sum()),
        "n_tested": len(tested), "monthly_t": mt, "months": n_months,
        "b1": oc.verdict_edge(pooled["exp_R"], pooled["t_stat"], share),
        "L": bh.stats(L, risk_pct), "O": bh.stats(O, risk_pct), "diff": diff, "welch_t": tw,
        "monthly_diff_t": mdt, "diff_months": mdn, "b2": oc.verdict_diff(diff, tw),
        "years": {y: bh.stats(v, risk_pct) for y, v in sorted(years.items())},
        "long": bh.stats([t for t in trades if t["side"] > 0], risk_pct),
        "short": bh.stats([t for t in trades if t["side"] < 0], risk_pct),
        "gross": bh.stats(trades, risk_pct, col="gross_R"),
        "cost_R": float(np.mean([t["cost_R"] for t in trades])) if trades else np.nan,
        "funding_R": float(np.mean([t["funding_R"] for t in trades])) if trades else np.nan,
        "spearman_hl": oc.spearman(tested.set_index("symbol")["exp_R"],
                                   pd.Series(hl_exp).reindex(tested["symbol"]).set_axis(tested["symbol"])),
    }


def _row(name, s):
    return (f"| {name} | {s['trades']} | {oc.fmt(s['exp_R'])} | "
            f"{oc.fmt(s['win_rate'] * 100 if s['trades'] else np.nan, 1, False)}% | "
            f"{oc.fmt(s['profit_factor'], 2, False)} | {oc.fmt(s['t_stat'], 2)} | {oc.fmt(s['total_R'], 1)} |")


def write_report(out, a, coins, plan):
    head = "| | Trx | Exp R | Win | PF | t | Total R |\n|---|---|---|---|---|---|---|"
    L = ["# Uji B — MEX di Binance perp, Jan 2020 → Mei 2023 (OOS waktu)\n",
         f"Dibuat {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC oleh `tools/oos_binance_pre2023.py`. "
         f"Rencana & kriteria: `docs/OOS_PLAN.md` §4, commit `{plan}`.\n",
         f"Transaksi yang **masuk** {WIN_START:%Y-%m-%d} → {WIN_END - pd.Timedelta(days=1):%Y-%m-%d}. "
         f"Data `data.binance.vision` (USDT-M perp 4H + funding 8 jam). Biaya: fee {FEE_PCT}% + "
         f"slippage {SLIP_PCT}% per sisi + funding asli. Parameter = `config.yaml`, tidak diubah.\n",
         f"## B1 — edge strategi: **{a['b1']}**\n",
         f"Kriteria: expectancy gabungan > 0, t ≥ {oc.T_PASS}, dan ≥ {oc.MIN_POSITIVE:.0%} koin "
         "positif → LULUS; expectancy > 0 tapi syarat lain kurang → TIDAK JELAS; ≤ 0 → GAGAL.\n",
         head, _row(f"Semua ({a['n_tested']} koin)", a["pooled"]),
         _row("Long", a["long"]), _row("Fade short", a["short"]), "",
         f"- Koin dengan expectancy > 0: **{a['n_positive']}/{a['n_tested']}** ({oc.fmt(a['share_positive'] * 100, 0, False)}%)",
         f"- Sebelum biaya {oc.fmt(a['gross']['exp_R'])} R; fee+slippage {oc.fmt(a['cost_R'])} R; "
         f"funding {oc.fmt(a['funding_R'])} R per trx",
         f"- t dari total R per bulan: {oc.fmt(a['monthly_t'], 2)} ({a['months']} bulan) — transaksi antar "
         "koin berkorelasi, jadi ini pembanding yang lebih konservatif dari t per transaksi",
         f"- Patokan: backtest Hyperliquid 30 koin +0,105 R; ETH Binance 2023–26 +0,31 R\n",
         "### Per tahun (tahun entry)\n", head]
    for y, s in a["years"].items():
        L.append(_row(str(y), s))
    L += ["", f"## B2 — kelompok koin live (L) vs lainnya (O): **{a['b2']}**\n",
          f"Kriteria sama dengan Uji A: selisih L − O > 0 dan t Welch ≥ {oc.T_PASS} → LULUS.\n",
          head, _row("L — koin live", a["L"]), _row("O — lainnya", a["O"]), "",
          f"**Selisih L − O: {oc.fmt(a['diff'])} R/trx, t Welch {oc.fmt(a['welch_t'], 2)}** "
          f"(t per bulan {oc.fmt(a['monthly_diff_t'], 2)}, {a['diff_months']} bulan).\n",
          f"Spearman expectancy per koin Binance 2020–23 vs Hyperliquid 2024–26: "
          f"**{oc.fmt(a['spearman_hl'], 2)}**.\n",
          "## Per koin\n",
          "| Koin | Kel. | Status | Binance | Bar | Celah | Bulan funding | Trx | Exp R | t | Profit @1% | Exp R HL 2024–26 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in coins.sort_values(["status", "exp_R"], ascending=[True, False]).itertuples():
        L.append(f"| {r.symbol} | {r.group} | {r.status} | {r.binance} | {r.bars} | {r.gaps} | "
                 f"{r.funding_months}/{r.kline_months} | {r.trades} | {oc.fmt(r.exp_R)} | "
                 f"{oc.fmt(r.t_stat, 2)} | {oc.fmt(r.return_pct, 1)}% | {oc.fmt(r.hl_exp_R)} |")
    L += ["", "## File\n", "- `summary.csv` — tabel per koin\n- `trades.csv` — semua transaksi di jendela\n"
          "- `data/` — cache arsip Binance (tidak di-commit; hapus atau `--refresh` untuk unduh ulang)"]
    with open(os.path.join(out, "REPORT.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


def main(argv=None):
    import oos_selection
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "backtest", "oos", "pre2023"))
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)
    data_dir = os.path.join(args.out, "data")
    os.makedirs(data_dir, exist_ok=True)
    p = config.load()["params"]
    hl_sum = pd.read_csv(os.path.join(ROOT, "backtest", "hyperliquid", "summary.csv"))
    hl_exp = dict(zip(hl_sum["symbol"], hl_sum["exp_R"]))

    rows, all_trades = [], []
    for sym in oc.ELIGIBLE:
        bsym = binance_symbol(sym)
        df, rates, km, fm = download(bsym, data_dir, args.refresh)
        bars = int(((df["ts"] >= WIN_START) & (df["ts"] < WIN_END)).sum()) if len(df) else 0
        row = {"symbol": sym, "group": "L" if sym in oc.GROUP_L else "O", "binance": bsym,
               "bars": bars, "gaps": bh.gap_count(df) if len(df) else 0,
               "first_bar": df["ts"].iloc[0].isoformat() if len(df) else "",
               "kline_months": len(km), "funding_months": len(fm), "still_open": 0,
               "hl_exp_R": hl_exp.get(sym, np.nan)}
        if bars < MIN_BARS:
            row["status"] = "tidak ada data" if bars == 0 else f"< {MIN_BARS} bar"
            row.update(bh.stats([], p.risk_pct))
        else:
            trades, row["still_open"] = run_coin(sym, df, rates, p)
            row["status"] = "diuji"
            row.update(bh.stats(trades, p.risk_pct))
            all_trades += trades
        rows.append(row)
        print(f"  {sym:5} {bsym:13} {row['status']:15} bar {bars:5}  trx {row['trades']:4}  "
              f"exp {oc.fmt(row['exp_R'])}")

    coins = pd.DataFrame(rows)
    a = analyse(all_trades, coins, p.risk_pct, hl_exp)
    plan = oos_selection.plan_commit()
    coins.to_csv(os.path.join(args.out, "summary.csv"), index=False)
    pd.DataFrame(all_trades).to_csv(os.path.join(args.out, "trades.csv"), index=False)
    with open(os.path.join(args.out, "run_meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"generated_utc": pd.Timestamp.now(tz="UTC").isoformat(), "plan_commit": plan,
                   "window": [WIN_START.isoformat(), WIN_END.isoformat()],
                   "fee_pct": FEE_PCT, "slip_pct": SLIP_PCT, "params": p.__dict__,
                   "trades_still_open_at_data_end": int(coins["still_open"].sum())},
                  fh, indent=2, default=str)
    write_report(args.out, a, coins, plan)
    print(f"\nB1: {a['pooled']['trades']} trx, exp {oc.fmt(a['pooled']['exp_R'])} R, "
          f"t {oc.fmt(a['pooled']['t_stat'], 2)}, positif {a['n_positive']}/{a['n_tested']} -> {a['b1']}")
    print(f"B2: L {oc.fmt(a['L']['exp_R'])} vs O {oc.fmt(a['O']['exp_R'])}, "
          f"t {oc.fmt(a['welch_t'], 2)} -> {a['b2']}")


if __name__ == "__main__":
    main()
