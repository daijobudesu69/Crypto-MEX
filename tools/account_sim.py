"""$100 account simulations behind backtest/oos/REPORT.md §5 (exploratory).

    python tools/account_sim.py      # after tools/bear_filter_study.py

Replays the V0 trades of tools/bear_filter_study.py (16 coins, Binance perp,
2020-01 -> 2026-08, every signal) through the live sizing rules:

  * risk = risk_pct of the balance at entry (compounded)
  * order under $10 raised to $10, skipped if that is > 2x the target risk
  * isolated 4x: skipped when the margin does not fit in 95% of free balance
  * circuit breaker: balance >= limit below its highest value -> no new entries

and the "pause, then resume" rules discussed after the OOS tests. These rules
were chosen after seeing the data; the numbers describe the past, they do not
validate a rule.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402

OUT = os.path.join(ROOT, "backtest", "oos", "regime")
T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
HALVING_JAN = [T("2024-01-01"), T("2028-01-01")]


def load():
    d = pd.read_csv(os.path.join(OUT, "trades_V0.csv"))
    d["en"] = pd.to_datetime(d["entry_time"], utc=True)
    d["ex"] = pd.to_datetime(d["exit_time"], utc=True)
    return d


def simulate(d, risk=0.01, breaker=None, resume=(), start=None, years=None,
             cash=100.0, lev=4):
    """Returns (final balance, max drawdown, lowest balance, event log)."""
    if start is not None:
        d = d[d["en"] >= T(start)]
    ev = sorted([(r.en, 1, i) for i, r in d.iterrows()] +
                [(r.ex, 0, i) for i, r in d.iterrows()], key=lambda e: (e[0], e[1]))
    bal = peak = cash
    open_, paused, log, path = {}, None, [], [cash]
    for ts, kind, i in ev:
        r = d.loc[i]
        if kind == 0:
            if i in open_:
                n, _ = open_.pop(i)
                bal += r.net_R * n * (r.r_price / r.entry_price)
                peak = max(peak, bal)
                path.append(bal)
                if breaker and paused is None and 1 - bal / peak >= breaker:
                    paused = ts
                    log.append(f"stop {ts:%Y-%m} ${bal:.0f}")
            continue
        if years is not None and ts.year not in years:
            continue
        if paused is not None:
            nxt = [x for x in resume if x > paused]
            if not nxt or ts < nxt[0]:
                continue
            paused, peak = None, bal
            log.append(f"mulai {ts:%Y-%m}")
        stop = r.r_price / r.entry_price
        target = bal * risk
        n = target / stop
        if n < 10:
            if 10 * stop > 2 * target:
                continue
            n = 10
        if n / lev > 0.95 * (bal - sum(m for _, m in open_.values())):
            continue
        open_[i] = (n, n / lev)
    s = pd.Series(path)
    return bal, float((1 - s / s.cummax()).max()), float(s.min()), log


def main():
    d = load()
    rows = []

    def add(group, name, **kw):
        b, dd, lo, log = simulate(d, **kw)
        rows.append({"kelompok": group, "skenario": name, "akhir_usd": round(b),
                     "dd_maks_pct": round(dd * 100), "terendah_usd": round(lo),
                     "catatan": "; ".join(log)})
        print(f"{group:12} {name:42} ${b:6.0f}  DD {dd:.0%}  min ${lo:4.0f}  {'; '.join(log)}")

    for s in ["2020-01-01", "2021-01-01", "2021-11-01", "2022-01-01", "2023-01-01",
              "2024-01-01", "2025-01-01", "2026-01-01"]:
        add("mulai", f"mulai {s[:7]}, ikut terus", start=s)
        add("mulai", f"mulai {s[:7]}, breaker 40% tanpa reset", start=s, breaker=0.40)
    add("siklus", "S0 ikut terus")
    add("siklus", "S1 breaker 40%, berhenti", breaker=0.40)
    add("siklus", "S2 breaker 40%, masuk lagi 1 Jan thn halving", breaker=0.40, resume=HALVING_JAN)
    add("siklus", "S2' masuk lagi tgl halving", breaker=0.40,
        resume=[T("2024-04-20"), T("2028-04-15")])
    add("siklus", "S4 kalender 2020-21 & 2024-25", years={2020, 2021, 2024, 2025})
    for risk, brk in [(0.01, None), (0.005, None), (0.01, 0.40), (0.005, 0.40),
                      (0.01, 0.30), (0.005, 0.30)]:
        lab = f"risiko {risk:.1%}" + (f", breaker {brk:.0%}" if brk else ", ikut terus")
        add("risiko", lab, risk=risk, breaker=brk)
        if brk:
            add("risiko", lab + " + masuk 2024", risk=risk, breaker=brk, resume=HALVING_JAN)
    pd.DataFrame(rows).to_csv(os.path.join(OUT, "account_sim.csv"), index=False)


if __name__ == "__main__":
    main()
