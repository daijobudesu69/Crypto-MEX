"""Head-to-head: the forward-test coins on Hyperliquid perp vs Binance USD-M perp.

    python tools/h2h_hl_binance.py

Both venues are cut to the exact same set of 4H bars (the timestamp
intersection), so warmup, window and bar count are identical and the only thing
that differs is where the candles came from. Binance comes from the official
data.binance.vision archive: monthly files, then daily files for the current
month. fapi.binance.com itself is geo-blocked from here (see mex/datafeed.py).

Costs are each venue's own tier-0 taker fee plus the same slippage. Funding is
left out on BOTH sides: Binance publishes funding only as monthly archives, so
the current month would be missing on one side only.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import mex.compat  # noqa: F401,E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402

import backtest_hyperliquid as bh  # noqa: E402
from mex import config, datafeed  # noqa: E402
from mex.strategy import compute_features  # noqa: E402

VISION = "https://data.binance.vision/data/futures/um"
FEE = {"hyperliquid": 0.045, "binance": 0.05}
SLIP_PCT = 0.01


def fetch_binance(symbol: str, start: pd.Timestamp, cache_dir: str, refresh: bool) -> pd.DataFrame:
    today = pd.Timestamp.now(tz="UTC").normalize()
    month0 = today.replace(day=1)
    urls = [f"{VISION}/monthly/klines/{symbol}/4h/{symbol}-4h-{m:%Y-%m}.zip"
            for m in pd.date_range(start.normalize().replace(day=1), month0 - pd.Timedelta(days=1),
                                   freq="MS")]
    urls += [f"{VISION}/daily/klines/{symbol}/4h/{symbol}-4h-{d:%Y-%m-%d}.zip"
             for d in pd.date_range(month0, today - pd.Timedelta(days=1), freq="D")]
    parts, missing = [], []
    for url in urls:
        path = os.path.join(cache_dir, url.rsplit("/", 1)[1])
        if not os.path.exists(path) or refresh:
            r = requests.get(url, headers=bh.UA, timeout=30)
            if r.status_code == 404:
                missing.append(url.rsplit("/", 1)[1])
                continue
            r.raise_for_status()
            with open(path, "wb") as fh:
                fh.write(r.content)
            time.sleep(0.05)
        with open(path, "rb") as fh:
            parts.append(bh.read_binance_zip(fh.read()))
    if missing:
        print(f"    {symbol}: belum ada di arsip -> {', '.join(missing)}")
    return bh.binance_frame(parts)


def align(a: pd.DataFrame, b: pd.DataFrame):
    common = pd.Index(a["ts"]).intersection(pd.Index(b["ts"]))
    a = a[a["ts"].isin(common)].reset_index(drop=True)
    b = b[b["ts"].isin(common)].reset_index(drop=True)
    return a, b


def signal_set(f, df):
    ts = df["ts"]
    return {(t, "L") for t in ts[f["long_signal"]]} | {(t, "S") for t in ts[f["fade_signal"]]}


def why_missing(f, i, side, p) -> list:
    """Which entry conditions are false at bar i on the venue that did NOT signal."""
    out = []
    if not f["volume"][i] > f["vol_avg"][i] * p.vol_mult:
        out.append("volume")
    if not f["high"][i] > f["prior_high"][i]:
        out.append("breakout harga")
    up = f["ema_fast"][i] > f["ema_slow"][i]
    if (side == "L") != up:
        out.append("tren EMA")
    if side == "L":
        if not (f["rsi_roc"][i] > 0 and f["rsi"][i] > p.rsi_confirm):
            out.append("RSI")
    elif not (f["rsi"][i] < f["prior_rsi_peak"][i] or f["rsi_roc"][i] < 0):
        out.append("RSI")
    return out


def compare(symbol, hl_df, bn_df, p):
    a, b = align(hl_df, bn_df)
    fa, fb = compute_features(a, p), compute_features(b, p)
    sa, sb = signal_set(fa, a), signal_set(fb, b)
    pos = {t: i for i, t in enumerate(a["ts"])}
    reasons = {"hanya_hl": {}, "hanya_binance": {}}
    for key, only, f_other in (("hanya_hl", sa - sb, fb), ("hanya_binance", sb - sa, fa)):
        for t, side in only:
            for r in why_missing(f_other, pos[t], side, p):
                reasons[key][r] = reasons[key].get(r, 0) + 1

    ta, _ = bh.run_backtest(a, p, FEE["hyperliquid"], SLIP_PCT, symbol)
    tb, _ = bh.run_backtest(b, p, FEE["binance"], SLIP_PCT, symbol)
    ka = {(t["entry_time"], t["side"]): t for t in ta}
    kb = {(t["entry_time"], t["side"]): t for t in tb}
    both = ka.keys() & kb.keys()
    diff = [ka[k]["net_R"] - kb[k]["net_R"] for k in both]
    same_sign = [np.sign(ka[k]["net_R"]) == np.sign(kb[k]["net_R"]) for k in both]

    close_diff = np.abs(a["close"] / b["close"] - 1) * 100
    atr_diff = np.abs(fa["atr"] / fb["atr"] - 1) * 100
    return {
        "symbol": symbol, "start": a["ts"].iloc[0], "end": a["ts"].iloc[-1], "bars": len(a),
        "gaps_hl": bh.gap_count(a), "gaps_binance": bh.gap_count(b),
        "median_close_diff_pct": float(np.nanmedian(close_diff)),
        "median_atr_diff_pct": float(np.nanmedian(atr_diff)),
        "signals_hl": len(sa), "signals_binance": len(sb), "signals_both": len(sa & sb),
        "reasons": reasons,
        "trades_both": len(both),
        "matched_mean_abs_R_diff": float(np.mean(np.abs(diff))) if diff else np.nan,
        "matched_same_outcome_pct": 100 * float(np.mean(same_sign)) if same_sign else np.nan,
        "hl": bh.stats(ta, p.risk_pct), "binance": bh.stats(tb, p.risk_pct),
        "trades_hl": ta, "trades_binance": tb,
    }


def write_report(out, rows, p):
    L = ["# Head-to-head — Hyperliquid vs Binance perp, koin forward test\n",
         f"Dibuat {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC oleh `tools/h2h_hl_binance.py`. "
         "Parameter = `config.yaml`. Kedua venue dipotong ke **himpunan bar 4H yang persis sama** "
         "(irisan timestamp) — jendela, jumlah bar dan warmup identik; yang beda hanya sumber candle.\n",
         f"Biaya: fee taker tier 0 masing-masing (Hyperliquid {FEE['hyperliquid']}%, "
         f"Binance {FEE['binance']}%) + slippage {SLIP_PCT}% per sisi. **Funding tidak dihitung "
         "di kedua sisi** — arsip funding Binance hanya bulanan, jadi bulan berjalan akan hilang "
         "di satu sisi saja. Di backtest 30 koin, funding Hyperliquid rata-rata cuma -0.007 R/trx.\n",
         "## Hasil per koin\n",
         "| Koin | Venue | Trx | Win | Rata2/trx | **Profit @risiko 1%** | Max DD @1% "
         "| **Profit modal penuh 1x** | Max DD 1x | Exp R | PF |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows + [combined(rows, p)]:
        for venue, name in (("hl", "Hyperliquid"), ("binance", "Binance")):
            s = r[venue]
            L.append(f"| {r['symbol'] if venue == 'hl' else ''} | {name} | {s['trades']} | "
                     f"{bh._pct(s['win_rate'])} | {s['avg_trade_pct']:+.2f}% | "
                     f"**{s['return_pct']:+.1f}%** | {s['max_dd_pct']:.1f}% | "
                     f"**{s['return_1x_pct']:+.1f}%** | {s['max_dd_1x_pct']:.1f}% | "
                     f"{bh._r(s['exp_R'], 3)} | {bh._pf(s['profit_factor'])} |")
    L.append(f"\n`GABUNGAN`: kolom risiko 1% = semua transaksi {len(rows)} koin berurutan menurut "
             "waktu exit, tiap transaksi risiko 1% modal. Kolom modal penuh 1x = modal dibagi "
             f"rata {100 // len(rows)}% per koin (tidak bisa 100% ke tiap koin karena posisinya "
             "sering terbuka bersamaan).\n")
    L.append("## Periode dan kualitas data\n")
    L.append("| Koin | Dari | Sampai | Bars (sama) | Bar hilang HL / BN | Beda close (median) | Beda ATR (median) |")
    L.append("|---|---|---|---|---|---|---|")
    for r in rows:
        L.append(f"| {r['symbol']} | {r['start']:%Y-%m-%d %H:%M} | {r['end']:%Y-%m-%d %H:%M} | "
                 f"{r['bars']} | {r['gaps_hl']} / {r['gaps_binance']} | "
                 f"{r['median_close_diff_pct']:.3f}% | {r['median_atr_diff_pct']:.2f}% |")
    L.append("\n## Sinyal — seberapa sama?\n")
    L.append("| Koin | Sinyal HL | Sinyal BN | Sama persis (bar + arah) | Hanya HL | Hanya BN "
             "| Transaksi identik | Hasil searah (untung/rugi sama) | Selisih R rata2 di transaksi identik |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        union = r["signals_hl"] + r["signals_binance"] - r["signals_both"]
        L.append(f"| {r['symbol']} | {r['signals_hl']} | {r['signals_binance']} | "
                 f"{r['signals_both']} ({100 * r['signals_both'] / union:.0f}%) | "
                 f"{r['signals_hl'] - r['signals_both']} | {r['signals_binance'] - r['signals_both']} | "
                 f"{r['trades_both']} | {r['matched_same_outcome_pct']:.0f}% | "
                 f"{r['matched_mean_abs_R_diff']:.3f} R |")
    L.append("\n**Kenapa sinyal beda?** Untuk tiap sinyal yang hanya muncul di satu venue, "
             "syarat mana yang gagal di venue satunya (satu sinyal bisa gagal >1 syarat):\n")
    L.append("| Koin | Sinyal hanya di HL — gagal di BN karena | Sinyal hanya di BN — gagal di HL karena |")
    L.append("|---|---|---|")
    fmt = lambda d: ", ".join(f"{k} {v}" for k, v in sorted(d.items(), key=lambda x: -x[1])) or "—"  # noqa: E731
    for r in rows:
        L.append(f"| {r['symbol']} | {fmt(r['reasons']['hanya_hl'])} | {fmt(r['reasons']['hanya_binance'])} |")
    with open(os.path.join(out, "REPORT.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


def equal_split_1x(rows, key) -> tuple[float, float]:
    """Capital split equally across coins, each slice compounding its own trades 1x.

    Compounding all coins' trades in one sequence would put 100% of capital into
    trades that are open at the same time, which no account can do.
    """
    curves = []
    for r in rows:
        tr = r[key]
        t = pd.to_datetime([x["exit_time"] for x in tr], utc=True)
        frac = [x["net_R"] * x["r_price"] / x["entry_price"] for x in tr]
        curves.append(pd.Series(np.cumprod(1 + np.array(frac)), index=t).groupby(level=0).last())
    eq = pd.concat(curves, axis=1).sort_index().ffill().fillna(1.0).mean(axis=1)
    return float((eq.iloc[-1] - 1) * 100), float((1 - eq / eq.cummax()).max() * 100)


def combined(rows, p):
    out = {"symbol": "**GABUNGAN**"}
    for venue, key in (("hl", "trades_hl"), ("binance", "trades_binance")):
        tr = sorted((t for r in rows for t in r[key]), key=lambda t: t["exit_time"])
        out[venue] = bh.stats(tr, p.risk_pct)
        out[venue]["return_1x_pct"], out[venue]["max_dd_1x_pct"] = equal_split_1x(rows, key)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "backtest", "h2h"))
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)
    p = config.load()["params"]
    hl_cache = os.path.join(ROOT, "backtest", "hyperliquid", "data")
    bn_cache = os.path.join(args.out, "data")
    os.makedirs(hl_cache, exist_ok=True)
    os.makedirs(bn_cache, exist_ok=True)

    rows = []
    for sym in datafeed.SYMBOLS:
        coin = sym.removesuffix("USDT")
        hl_df = bh.fetch_candles(coin, hl_cache, args.refresh)
        bn_df = fetch_binance(sym, hl_df["ts"].iloc[0], bn_cache, args.refresh)
        r = compare(coin, hl_df, bn_df, p)
        rows.append(r)
        print(f"{coin:5} {r['bars']} bar {r['start']:%Y-%m-%d}->{r['end']:%Y-%m-%d %H:%M}  "
              f"HL {r['hl']['return_pct']:+6.1f}% ({r['hl']['trades']} trx)  "
              f"BN {r['binance']['return_pct']:+6.1f}% ({r['binance']['trades']} trx)")

    trades = [t | {"venue": v} for r in rows
              for v, k in (("hyperliquid", "trades_hl"), ("binance", "trades_binance")) for t in r[k]]
    pd.DataFrame(trades).to_csv(os.path.join(args.out, "trades.csv"), index=False)
    flat = [{"symbol": r["symbol"], "start": r["start"], "end": r["end"], "bars": r["bars"],
             **{f"hl_{k}": v for k, v in r["hl"].items()},
             **{f"bn_{k}": v for k, v in r["binance"].items()}} for r in rows]
    pd.DataFrame(flat).to_csv(os.path.join(args.out, "summary.csv"), index=False)
    write_report(args.out, rows, p)
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
