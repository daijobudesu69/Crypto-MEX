"""Diagnostic: does MEX's edge follow measurable market behaviour, era by era?

    python tools/regime_diagnostic.py

Question from the owner after the OOS tests: is 2020-2023 (retail-driven) a fair
stand-in for 2024+ (ETFs, institutions)? Instead of assuming, measure it. One
venue (Binance USDT-M perp), one continuous 4H series per coin from 2020-01 to
2026-08, so venue and coin mix cannot explain any difference.

Per coin per calendar quarter, from candles alone (no strategy):
  cont5    mean (close[t+5] - close[t]) / ATR[t] over MEX breakout bars
           (high > prior 20-bar high and volume > 1.5x average)
  fail3    share of those breakouts that close back below the breakout level
           within 3 bars
  er30     mean Kaufman efficiency ratio over 30 bars (trendiness)
  ac1      lag-1 autocorrelation of 4H log returns (momentum vs reversion)
  atr_pct  mean ATR / close
and the MEX expectancy (net R, Binance fee + slippage + real funding) of trades
entered in that quarter.

This is descriptive. It does NOT create a filter: a regime rule chosen after
looking at these results would need its own pre-registration and fresh data.
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import mex.compat  # noqa: F401,E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import backtest_hyperliquid as bh  # noqa: E402
import oos_binance_pre2023 as ob  # noqa: E402
import oos_common as oc  # noqa: E402
from mex import config  # noqa: E402
from mex.strategy import compute_features  # noqa: E402

PRE = os.path.join(ROOT, "backtest", "oos", "pre2023", "data")
LOCAL = os.path.join(os.path.dirname(ROOT), "Backtest MEX 2.0 Strategy", "data")
OUT = os.path.join(ROOT, "backtest", "oos", "regime")
END = pd.Timestamp("2026-09-01", tz="UTC")
COINS = ["ETH", "XRP", "SOL", "DOGE", "SHIB", "LINK", "NEAR", "BNB", "TRX", "ADA",
         "XLM", "BCH", "UNI", "LTC", "AVAX", "HBAR"]   # 2020-01 -> 2026-08 on Binance
ERAS = [("2020-21 (bull retail)", "2020-01-01", "2022-01-01"),
        ("2022-23 (bear, pra-ETF)", "2022-01-01", "2024-01-01"),
        ("2024-26 (pasca-ETF)", "2024-01-01", "2026-09-01")]


def load(sym):
    bsym = ob.binance_symbol(sym)
    k = pd.read_csv(os.path.join(PRE, f"{bsym}_4h.csv.gz"))
    k["ts"] = pd.to_datetime(k["ts"], utc=True, format="ISO8601")
    later = bh.load_binance_archive(os.path.join(LOCAL, "klines"), sym)
    df = (pd.concat([k, later]).drop_duplicates("ts").sort_values("ts").reset_index(drop=True))
    df = df[df["ts"] < END].reset_index(drop=True)
    f = pd.read_csv(os.path.join(PRE, f"{bsym}_funding.csv.gz"))
    f["time"] = pd.to_datetime(f["time"], utc=True, format="ISO8601")
    parts = [f]
    for fp in sorted(glob.glob(os.path.join(LOCAL, "funding", bsym, "*.zip"))):
        with open(fp, "rb") as fh:
            parts.append(ob.read_funding_zip(fh.read()))
    rates = pd.concat(parts).drop_duplicates("time").sort_values("time").set_index("time")["rate"]
    return df, rates


def features(df, p):
    f = compute_features(df, p)
    c, atr = f["close"], f["atr"]
    n = len(c)
    fwd5 = np.full(n, np.nan)
    fwd5[:-5] = (c[5:] - c[:-5]) / atr[:-5]
    lvl = f["prior_high"]
    min3 = pd.Series(c).shift(-1).rolling(3).min().shift(-2).to_numpy()   # min close t+1..t+3
    fail = np.where(np.isfinite(min3), (min3 < lvl).astype(float), np.nan)
    ch = np.abs(np.diff(c, prepend=np.nan))
    er = np.abs(c - np.roll(c, 30)) / pd.Series(ch).rolling(30).sum().to_numpy()
    er[:30] = np.nan
    lr = np.diff(np.log(c), prepend=np.nan)
    bo = f["breakout_up"] & np.isfinite(atr) & np.isfinite(lvl)
    d = pd.DataFrame({"ts": df["ts"], "bo": bo, "fwd5": np.where(bo, fwd5, np.nan),
                      "fail": np.where(bo, fail, np.nan), "er": er, "lr": lr,
                      "atrp": atr / c * 100})
    d["q"] = d["ts"].dt.tz_localize(None).dt.to_period("Q").astype(str)
    g = d.groupby("q")
    out = pd.DataFrame({
        "breakouts": g["bo"].sum(), "cont5": g["fwd5"].mean(), "fail3": g["fail"].mean(),
        "er30": g["er"].mean(), "atr_pct": g["atrp"].mean(),
        "ac1": g["lr"].apply(lambda x: x.autocorr(1))})
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    p = config.load()["params"]
    feats, trades = [], []
    for sym in COINS:
        df, rates = load(sym)
        gaps = bh.gap_count(df)
        tr, _ = bh.run_backtest(df, p, ob.FEE_PCT, ob.SLIP_PCT, sym)
        for t in tr:
            t["funding_R"] = bh.funding_r(t, rates)
            t["net_R"] += t["funding_R"]
        trades += tr
        fq = features(df, p).assign(symbol=sym)
        feats.append(fq)
        print(f"  {sym:5} {df['ts'].iloc[0]:%Y-%m} -> {df['ts'].iloc[-1]:%Y-%m}  bar {len(df)}  "
              f"celah {gaps}  trx {len(tr)}")
    F = pd.concat(feats).reset_index()
    T = pd.DataFrame(trades)
    T["q"] = pd.to_datetime(T["entry_time"], utc=True).dt.tz_localize(None).dt.to_period("Q").astype(str)
    cq = (T.groupby(["symbol", "q"])["net_R"].agg(trades="count", exp_R="mean").reset_index())
    CQ = F.merge(cq, on=["symbol", "q"], how="left")
    Q = (CQ.groupby("q").agg(cont5=("cont5", "mean"), fail3=("fail3", "mean"), er30=("er30", "mean"),
                             ac1=("ac1", "mean"), atr_pct=("atr_pct", "mean"))
         .join(T.groupby("q")["net_R"].agg(trades="count", exp_R="mean", total_R="sum")))
    Q = Q[Q.index >= "2020Q1"]
    feats_ = ["cont5", "fail3", "er30", "ac1", "atr_pct"]
    rho_q = {k: oc.spearman(Q[k], Q["exp_R"]) for k in feats_}
    cqv = CQ[CQ["trades"] >= 5]
    rho_cq = {k: oc.spearman(cqv[k], cqv["exp_R"]) for k in feats_}
    eras = []
    for name, a, b in ERAS:
        tt = T[(pd.to_datetime(T["entry_time"], utc=True) >= pd.Timestamp(a, tz="UTC"))
               & (pd.to_datetime(T["entry_time"], utc=True) < pd.Timestamp(b, tz="UTC"))]
        qs = Q[(Q.index >= pd.Period(a, "Q").strftime("%YQ%q")) & (Q.index < pd.Period(b, "Q").strftime("%YQ%q"))]
        eras.append({"era": name, "trades": len(tt), "exp_R": tt["net_R"].mean(),
                     "t": oc.t_stat(tt["net_R"]), **{k: qs[k].mean() for k in feats_},
                     "q_pos": f"{int((qs['exp_R'] > 0).sum())}/{len(qs)}"})
    Q.to_csv(os.path.join(OUT, "quarters.csv"))
    CQ.to_csv(os.path.join(OUT, "coin_quarters.csv"), index=False)
    pd.DataFrame(eras).to_csv(os.path.join(OUT, "eras.csv"), index=False)
    with open(os.path.join(OUT, "correlations.json"), "w", encoding="utf-8") as fh:
        json.dump({"quarter_level": rho_q, "coin_quarter_level_min5trx": rho_cq,
                   "n_quarters": len(Q), "n_coin_quarters": len(cqv)}, fh, indent=2)
    pd.set_option("display.width", 200)
    print("\nEra:\n", pd.DataFrame(eras).round(3).to_string(index=False))
    print("\nKuartal:\n", Q.round(3).to_string())
    print("\nSpearman vs exp_R, per kuartal:", {k: round(v, 2) for k, v in rho_q.items()})
    print("Spearman vs exp_R, per koin-kuartal (>=5 trx, n=%d):" % len(cqv),
          {k: round(v, 2) for k, v in rho_cq.items()})


if __name__ == "__main__":
    main()
