"""What if MEX had not traded during the 2022-23 bear market? (exploratory)

    python tools/bear_filter_study.py

Same 16 coins, Binance perp, 2020-01 -> 2026-08, as tools/regime_diagnostic.py.
Variants, each a full bar-by-bar replay with signals blocked (not trades deleted
afterwards -- a blocked signal frees the coin for the next one):

  V0  baseline: every signal
  V1  hindsight: no signals from 2022-01-01 to 2023-12-31. Unattainable; nobody
      knew the bear market's dates in advance. An upper bound only.
  V2  real-time: no signals while BTC's last completed daily close is below its
      200-day SMA (Binance spot BTCUSDT 1d)
  V3  real-time, longs only: as V2 but fade shorts stay allowed

Exploratory and chosen after seeing the OOS results, so it proves nothing on its
own. A rule taken from here would need pre-registration and forward data.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import mex.compat  # noqa: F401,E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import oos_binance_pre2023 as ob  # noqa: E402
import oos_common as oc  # noqa: E402
import regime_diagnostic as rd  # noqa: E402
from mex import config  # noqa: E402
from mex.strategy import compute_features, step  # noqa: E402

OUT = os.path.join(ROOT, "backtest", "oos", "regime")
BTC = os.path.join(OUT, "data", "BTCUSDT_spot_1d.csv.gz")
SMA_DAYS = 200
BEAR = (pd.Timestamp("2022-01-01", tz="UTC"), pd.Timestamp("2024-01-01", tz="UTC"))
VARIANTS = {"V0 semua sinyal": "base", "V1 hindsight: skip 2022–23": "hindsight",
            "V2 BTC > SMA200 (semua)": "btc_all", "V3 BTC > SMA200 (long saja)": "btc_long"}


def btc_regime() -> pd.DataFrame:
    """BTC above its SMA200, keyed by the moment that daily close became known."""
    d = pd.read_csv(BTC)
    d["day"] = pd.to_datetime(d["day"], utc=True, format="ISO8601")
    d["sma"] = d["close"].rolling(SMA_DAYS).mean()
    d["known_at"] = d["day"] + pd.Timedelta(days=1)       # daily bar closes at next 00:00
    d["bull"] = d["close"] > d["sma"]
    return d.dropna(subset=["sma"])[["known_at", "bull", "close", "sma"]]


def bull_at(ts: pd.Series, reg: pd.DataFrame) -> np.ndarray:
    """Regime as known when bar ts closes (ts + 4h). Unknown -> allowed."""
    close = pd.DataFrame({"t": ts + pd.Timedelta(hours=4)})
    m = pd.merge_asof(close, reg.rename(columns={"known_at": "t"}), on="t", direction="backward")
    return m["bull"].fillna(True).to_numpy(bool)


def replay(df, p, allow_long, allow_short, sym):
    """tools/backtest_hyperliquid.run_backtest with signals masked before the loop."""
    f = compute_features(df, p)
    f["long_signal"] = f["long_signal"] & allow_long
    f["fade_signal"] = f["fade_signal"] & allow_short
    ts = pd.DatetimeIndex(df["ts"])
    cost_frac = (ob.FEE_PCT + ob.SLIP_PCT) / 100.0
    pos, pending, out = None, None, []
    for i in range(len(df)):
        pos, pending, events = step(f, ts, i, p, pos, pending)
        for ev in events:
            if ev["event"] != "EXIT":
                continue
            ps, px = ev["pos"], float(ev["exit_price"])
            gross = ps.side * (px - ps.entry_price) / ps.r_usdt
            cost = cost_frac * (ps.entry_price + px) / ps.r_usdt
            out.append({"symbol": sym, "side": ps.side, "signal_time": ps.signal_bar,
                        "entry_time": ps.entry_bar, "exit_time": ev["bar"].isoformat(),
                        "entry_price": ps.entry_price, "exit_price": px, "r_price": ps.r_usdt,
                        "gross_R": gross, "cost_R": -cost, "net_R": gross - cost})
    return out


def summary(T: pd.DataFrame, years: float) -> dict:
    r = T["net_R"].to_numpy()
    d = T.assign(x=pd.to_datetime(T["exit_time"], utc=True)).sort_values("x")
    cum = d["net_R"].cumsum().to_numpy()
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    dd_i = int(np.argmax(peak - cum))
    return {"trades": len(r), "exp_R": r.mean(), "t": oc.t_stat(r), "total_R": r.sum(),
            "R_per_year": r.sum() / years, "max_dd_R": float((peak - cum).max()),
            "dd_trough": d["x"].iloc[dd_i].strftime("%Y-%m"),
            "worst_12m_R": worst_window(d, 365)}


def worst_window(d: pd.DataFrame, days: int) -> float:
    s = d.set_index("x")["net_R"].resample("D").sum()
    return float(s.rolling(f"{days}D").sum().min())


def main():
    p = config.load()["params"]
    reg = btc_regime()
    start, end = pd.Timestamp("2020-01-01", tz="UTC"), rd.END
    years = (end - start).days / 365.25
    res = {k: [] for k in VARIANTS}
    for sym in rd.COINS:
        df, rates = rd.load(sym)
        ts = df["ts"]
        bull = bull_at(ts, reg)
        bear_hs = ((ts >= BEAR[0]) & (ts < BEAR[1])).to_numpy()
        masks = {"base": (True, True), "hindsight": (~bear_hs, ~bear_hs),
                 "btc_all": (bull, bull), "btc_long": (bull, True)}
        for name, key in VARIANTS.items():
            tr = replay(df, p, *masks[key], sym)
            for t in tr:
                t["funding_R"] = ob.bh.funding_r(t, rates)
                t["net_R"] += t["funding_R"]
            res[name] += tr
        print(f"  {sym} selesai")
    rows, yearly = [], {}
    for name, tr in res.items():
        T = pd.DataFrame(tr)
        rows.append({"variant": name, **summary(T, years)})
        yearly[name] = T.groupby(pd.to_datetime(T["entry_time"], utc=True).dt.year)["net_R"].sum()
        T.to_csv(os.path.join(OUT, f"trades_{name.split()[0]}.csv"), index=False)
    S = pd.DataFrame(rows)
    Y = pd.DataFrame(yearly)
    S.to_csv(os.path.join(OUT, "bear_filter_summary.csv"), index=False)
    Y.to_csv(os.path.join(OUT, "bear_filter_yearly.csv"))
    # When was the real-time rule off?
    r = reg[(reg["known_at"] >= start) & (reg["known_at"] < end)].copy()
    r["run"] = (r["bull"] != r["bull"].shift()).cumsum()
    off = (r[~r["bull"]].groupby("run")["known_at"].agg(["min", "max"]))
    off["days"] = (off["max"] - off["min"]).dt.days + 1
    off = off[off["days"] >= 30]
    off.to_csv(os.path.join(OUT, "btc_below_sma200_runs.csv"), index=False)
    pd.set_option("display.width", 200)
    print("\n", S.round(3).to_string(index=False))
    print("\nTotal R per tahun entry:\n", Y.round(1).to_string())
    print(f"\nBTC di bawah SMA200: {(~r['bull']).mean():.0%} hari 2020-01..2026-08; periode >= 30 hari:")
    print(off.assign(min=off["min"].dt.date, max=off["max"].dt.date).to_string(index=False))


if __name__ == "__main__":
    main()
