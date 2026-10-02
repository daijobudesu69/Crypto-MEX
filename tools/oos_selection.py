"""Uji A (docs/OOS_PLAN.md §3): does picking coins by their backtest add value?

    python tools/oos_selection.py

The live universe was chosen by ranking coins on the whole Hyperliquid backtest
and keeping the top. That ranking was never tested on data it had not seen.
This script tests the *procedure*, not the coins: rank on a selection window,
keep the top 12 of the 24 eligible coins, and measure top vs bottom on the
window that follows. Walk-forward (A1) decides the verdict; the half split (A2)
is descriptive.

Data is the cached Hyperliquid candle + funding set used by
tools/backtest_hyperliquid.py. Nothing is downloaded: the test must run on the
exact data the selection was made on.
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import mex.compat  # noqa: F401,E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import backtest_hyperliquid as bh  # noqa: E402
import oos_common as oc  # noqa: E402
from mex import config  # noqa: E402

K_TOP = 12
# A1: (selection start, selection end = test start, test end). None = end of data.
WALK_FORWARD = [
    ("2024-06-16", "2025-06-16", "2025-09-16"),
    ("2024-09-16", "2025-09-16", "2025-12-16"),
    ("2024-12-16", "2025-12-16", "2026-03-16"),
    ("2025-03-16", "2026-03-16", "2026-06-16"),
    ("2025-06-16", "2026-06-16", None),
]
# A2: halves. H1 starts before the first bar so XLM's two extra bars are included.
SPLIT_START, SPLIT_MID = "2024-06-14", "2025-08-01"


def _ts(s):
    if s is None:
        return None
    t = pd.Timestamp(s)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def load_trades(data_dir: str, p, fee: float, slip: float) -> list:
    """Every closed trade on the 24 eligible coins, net of fees, slippage, funding."""
    out = []
    for sym, coin in oc.ELIGIBLE.items():
        path = os.path.join(data_dir, f"{coin}_{bh.INTERVAL}.csv.gz")
        if not os.path.exists(path):
            raise SystemExit(f"cache {path} tidak ada; Uji A tidak boleh mengunduh data baru")
        df = bh.fetch_candles(coin, data_dir, refresh=False)
        trades, _ = bh.run_backtest(df, p, fee, slip, sym)
        rates = bh.fetch_funding(coin, df["ts"].iloc[0], df["ts"].iloc[-1] + bh.BAR,
                                 data_dir, refresh=False)
        for t in trades:
            t["funding_R"] = bh.funding_r(t, rates)
            t["net_R"] += t["funding_R"]
        out += trades
    return out


def rank_coins(trades: list, start, end, risk_pct: float) -> pd.DataFrame:
    """Rank the eligible coins on trades entered AND exited inside [start, end).

    Same yardstick as the 28 Sep choice: compounded profit at 1% risk. A coin with
    no closed trade in the window scores 0. Ties go to the symbol, alphabetically,
    so the ranking is deterministic.
    """
    win = oc.in_window(trades, _ts(start), _ts(end), closed=True)
    rows = []
    for sym in oc.ELIGIBLE:
        st = bh.stats([t for t in win if t["symbol"] == sym], risk_pct)
        rows.append({"symbol": sym, "sel_return_pct": st["return_pct"],
                     "sel_exp_R": st["exp_R"], "sel_trades": st["trades"]})
    r = pd.DataFrame(rows).sort_values(["sel_return_pct", "symbol"], ascending=[False, True])
    r["sel_rank"] = np.arange(1, len(r) + 1)
    r["group"] = np.where(r["sel_rank"] <= K_TOP, "top", "bottom")
    return r.reset_index(drop=True)


def evaluate(trades: list, sel: tuple, test: tuple, risk_pct: float, label: str):
    """One selection -> test step. Returns (row for the window table, tagged test trades)."""
    ranking = rank_coins(trades, sel[0], sel[1], risk_pct)
    group = dict(zip(ranking["symbol"], ranking["group"]))
    test_tr = [dict(t, window=label, group=group[t["symbol"]])
               for t in oc.in_window(trades, _ts(test[0]), _ts(test[1]), closed=False)]
    top = [t for t in test_tr if t["group"] == "top"]
    bot = [t for t in test_tr if t["group"] == "bottom"]
    sel_tr = oc.in_window(trades, _ts(sel[0]), _ts(sel[1]), closed=True)
    top_syms = set(ranking.loc[ranking["group"] == "top", "symbol"])
    is_top = [t for t in sel_tr if t["symbol"] in top_syms]
    per_coin = pd.DataFrame(test_tr).groupby("symbol")["net_R"].mean() if test_tr else pd.Series(dtype=float)
    rk = ranking.set_index("symbol")
    rho = oc.spearman(-rk["sel_rank"], per_coin.reindex(rk.index))
    diff, tw = oc.welch([t["net_R"] for t in top], [t["net_R"] for t in bot])
    row = {"window": label, "sel_start": sel[0], "sel_end": sel[1],
           "test_start": test[0], "test_end": test[1] or "akhir data",
           "top_coins": " ".join(ranking.loc[ranking["group"] == "top", "symbol"]),
           "is_top_exp_R": bh.stats(is_top, risk_pct)["exp_R"],
           "top_trades": len(top), "top_exp_R": bh.stats(top, risk_pct)["exp_R"],
           "bottom_trades": len(bot), "bottom_exp_R": bh.stats(bot, risk_pct)["exp_R"],
           "all_exp_R": bh.stats(test_tr, risk_pct)["exp_R"],
           "diff_R": diff, "welch_t": tw, "spearman": rho}
    return row, test_tr, ranking.assign(window=label)


def summarise(test_tr: list, risk_pct: float) -> dict:
    top = [t for t in test_tr if t["group"] == "top"]
    bot = [t for t in test_tr if t["group"] == "bottom"]
    diff, tw = oc.welch([t["net_R"] for t in top], [t["net_R"] for t in bot])
    mt, months = oc.monthly_diff_t(top, bot)
    return {"top": bh.stats(top, risk_pct), "bottom": bh.stats(bot, risk_pct),
            "all": bh.stats(test_tr, risk_pct), "diff": diff, "welch_t": tw,
            "monthly_t": mt, "months": months, "verdict": oc.verdict_diff(diff, tw)}


def plan_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "log", "-1", "--format=%h %cI", "--", "docs/OOS_PLAN.md"],
            cwd=ROOT, text=True).strip()
    except Exception:  # noqa: BLE001
        return "tidak diketahui"


def parity_check(trades: list, summary_csv: str) -> list:
    """Per-coin totals must equal the original backtest: same data, same engine."""
    if not os.path.exists(summary_csv):
        return ["summary.csv backtest asli tidak ada; paritas tidak dicek"]
    ref = pd.read_csv(summary_csv).set_index("symbol")
    d = pd.DataFrame(trades).groupby("symbol")["net_R"].agg(["count", "sum"])
    bad = []
    for sym in oc.ELIGIBLE:
        r = ref.loc[sym]
        if int(d.loc[sym, "count"]) != int(r["trades"]) or not np.isclose(d.loc[sym, "sum"], r["total_R"]):
            bad.append(f"{sym}: {int(d.loc[sym, 'count'])} trx / {d.loc[sym, 'sum']:+.3f} R vs "
                       f"asli {int(r['trades'])} / {r['total_R']:+.3f} R")
    return bad


def _line(name, s):
    return (f"| {name} | {s['trades']} | {oc.fmt(s['exp_R'])} | "
            f"{oc.fmt(s['win_rate'] * 100 if s['trades'] else np.nan, 1, False)}% | "
            f"{oc.fmt(s['profit_factor'], 2, False)} | {oc.fmt(s['t_stat'], 2)} |")


def write_report(out, wf_rows, wf, split_rows, split, membership, parity, risk_pct, args):
    L = ["# Uji A — apakah memilih koin dari backtest menambah nilai?\n",
         f"Dibuat {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC oleh `tools/oos_selection.py`. "
         f"Rencana & kriteria: `docs/OOS_PLAN.md` §3, commit `{plan_commit()}`.\n",
         f"Universe 24 koin (hidup di Hyperliquid sejak Jun 2024). Seleksi = {K_TOP} teratas "
         "menurut profit @risiko 1% di jendela seleksi. Data = cache backtest Hyperliquid "
         f"(tidak diunduh ulang). Biaya: fee {args.fee_pct}% + slippage {args.slip_pct}% per sisi "
         "+ funding per jam asli.\n"]
    L.append("**Paritas dengan backtest asli:** " + (
        "✅ jumlah transaksi dan total R ke-24 koin sama persis dengan `backtest/hyperliquid/summary.csv`.\n"
        if not parity else "❌ " + "; ".join(parity) + "\n"))
    L.append(f"## A1 — walk-forward (uji utama): **{wf['verdict']}**\n")
    L.append(f"Transaksi OOS dari {len(wf_rows)} jendela uji disambung. Kriteria: selisih top − "
             f"bottom > 0 dan t Welch ≥ {oc.T_PASS} → LULUS; > 0 tapi t < {oc.T_PASS} → TIDAK JELAS; "
             "≤ 0 → GAGAL.\n")
    L.append("| Kelompok | Trx | Exp R | Win | PF | t |")
    L.append("|---|---|---|---|---|---|")
    L.append(_line(f"Top {K_TOP} (dipilih)", wf["top"]))
    L.append(_line(f"Bottom {len(oc.ELIGIBLE) - K_TOP}", wf["bottom"]))
    L.append(_line("Semua 24", wf["all"]))
    L.append("")
    L.append(f"**Selisih top − bottom: {oc.fmt(wf['diff'])} R/trx, t Welch {oc.fmt(wf['welch_t'], 2)}** "
             f"(t dari selisih rata-rata per bulan: {oc.fmt(wf['monthly_t'], 2)}, {wf['months']} bulan).\n")
    L.append("### Per jendela\n")
    L.append("| Jendela uji | Exp top (IS) | Exp top (OOS) | Exp bottom (OOS) | Selisih | t | Spearman | Top 12 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in wf_rows:
        L.append(f"| {r['test_start']} → {r['test_end']} | {oc.fmt(r['is_top_exp_R'])} | "
                 f"{oc.fmt(r['top_exp_R'])} ({r['top_trades']}) | {oc.fmt(r['bottom_exp_R'])} "
                 f"({r['bottom_trades']}) | {oc.fmt(r['diff_R'])} | {oc.fmt(r['welch_t'], 2)} | "
                 f"{oc.fmt(r['spearman'], 2)} | {r['top_coins']} |")
    wins = sum(1 for r in wf_rows if np.isfinite(r["diff_R"]) and r["diff_R"] > 0)
    rho = np.nanmean([r["spearman"] for r in wf_rows])
    is_mean = np.nanmean([r["is_top_exp_R"] for r in wf_rows])
    L.append("")
    L.append(f"Top > bottom di **{wins}/{len(wf_rows)}** jendela. Spearman rata-rata "
             f"**{oc.fmt(rho, 2)}** (ranking seleksi vs expectancy uji per koin). Expectancy top "
             f"di jendela seleksi rata-rata {oc.fmt(is_mean)} R → di jendela uji "
             f"{oc.fmt(wf['top']['exp_R'])} R.\n")
    L.append("## A2 — belah dua (deskriptif)\n")
    L.append("| Arah | Exp top | Exp bottom | Selisih | t | Spearman | Top 12 |")
    L.append("|---|---|---|---|---|---|---|")
    for r, s in zip(split_rows, split):
        L.append(f"| {r['window']} | {oc.fmt(s['top']['exp_R'])} ({s['top']['trades']}) | "
                 f"{oc.fmt(s['bottom']['exp_R'])} ({s['bottom']['trades']}) | {oc.fmt(s['diff'])} | "
                 f"{oc.fmt(s['welch_t'], 2)} | {oc.fmt(r['spearman'], 2)} | {r['top_coins']} |")
    L.append("\nH2 → H1 memakai data masa depan untuk memilih; ia hanya mengukur apakah ranking "
             "koin bertahan, bukan apakah ranking bisa memprediksi.\n")
    L.append("## Seberapa sering tiap koin masuk top (5 jendela A1)\n")
    L.append("| Koin | Kelompok live | Masuk top | Rata-rata ranking |")
    L.append("|---|---|---|---|")
    for r in membership.itertuples():
        L.append(f"| {r.symbol} | {'L' if r.symbol in oc.GROUP_L else 'O'} | {r.n_top}/{len(wf_rows)} | {r.mean_rank:.1f} |")
    L.append("\n## File\n\n- `windows.csv` — tabel per jendela (A1 + A2)\n"
             "- `rankings.csv` — ranking 24 koin di tiap jendela seleksi\n"
             "- `oos_trades.csv` — transaksi OOS A1 dengan jendela dan kelompoknya")
    with open(os.path.join(out, "REPORT.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default=os.path.join(ROOT, "backtest", "hyperliquid", "data"))
    ap.add_argument("--out", default=os.path.join(ROOT, "backtest", "oos", "selection"))
    ap.add_argument("--fee-pct", type=float, default=bh.DEFAULT_FEE_PCT)
    ap.add_argument("--slip-pct", type=float, default=bh.DEFAULT_SLIP_PCT)
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    p = config.load()["params"]

    trades = load_trades(args.data, p, args.fee_pct, args.slip_pct)
    parity = parity_check(trades, os.path.join(ROOT, "backtest", "hyperliquid", "summary.csv"))
    print(f"{len(trades)} transaksi di {len(oc.ELIGIBLE)} koin; paritas: "
          + ("OK" if not parity else f"{len(parity)} beda"))

    wf_rows, wf_trades, rankings = [], [], []
    for k, (s0, s1, t1) in enumerate(WALK_FORWARD, 1):
        row, tt, rk = evaluate(trades, (s0, s1), (s1, t1), p.risk_pct, f"WF{k}")
        wf_rows.append(row)
        wf_trades += tt
        rankings.append(rk)
        print(f"  WF{k} uji {s1} -> {t1 or 'akhir'}: top {oc.fmt(row['top_exp_R'])}  "
              f"bottom {oc.fmt(row['bottom_exp_R'])}  rho {oc.fmt(row['spearman'], 2)}")
    wf = summarise(wf_trades, p.risk_pct)

    split_rows, split = [], []
    for label, sel, test in (("H1 → H2", (SPLIT_START, SPLIT_MID), (SPLIT_MID, None)),
                             ("H2 → H1", (SPLIT_MID, None), (SPLIT_START, SPLIT_MID))):
        row, tt, rk = evaluate(trades, sel, test, p.risk_pct, label)
        split_rows.append(row)
        split.append(summarise(tt, p.risk_pct))
        rankings.append(rk)

    rk_all = pd.concat(rankings, ignore_index=True)
    wf_rk = rk_all[rk_all["window"].str.startswith("WF")]
    membership = (wf_rk.assign(is_top=wf_rk["group"] == "top")
                  .groupby("symbol").agg(n_top=("is_top", "sum"), mean_rank=("sel_rank", "mean"))
                  .reset_index().sort_values(["mean_rank", "symbol"]))

    pd.DataFrame(wf_rows + split_rows).to_csv(os.path.join(args.out, "windows.csv"), index=False)
    rk_all.to_csv(os.path.join(args.out, "rankings.csv"), index=False)
    pd.DataFrame(wf_trades).to_csv(os.path.join(args.out, "oos_trades.csv"), index=False)
    with open(os.path.join(args.out, "run_meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"generated_utc": pd.Timestamp.now(tz="UTC").isoformat(),
                   "plan_commit": plan_commit(), "args": vars(args), "k_top": K_TOP,
                   "walk_forward": WALK_FORWARD, "split": [SPLIT_START, SPLIT_MID],
                   "params": p.__dict__, "parity_issues": parity}, fh, indent=2, default=str)
    write_report(args.out, wf_rows, wf, split_rows, split, membership, parity, p.risk_pct, args)
    print(f"\nA1 walk-forward: top {oc.fmt(wf['top']['exp_R'])} R ({wf['top']['trades']} trx), "
          f"bottom {oc.fmt(wf['bottom']['exp_R'])} R ({wf['bottom']['trades']} trx), "
          f"selisih {oc.fmt(wf['diff'])}, t {oc.fmt(wf['welch_t'], 2)} -> {wf['verdict']}")


if __name__ == "__main__":
    main()
