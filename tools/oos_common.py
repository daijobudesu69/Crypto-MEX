"""Shared pieces of the OOS tests pre-registered in docs/OOS_PLAN.md.

Everything that decides a verdict lives here so both tests apply the same
universe, the same statistics and the same pass/fail thresholds. Changing a
constant below after the tests have run changes the pre-registered plan; record
it in the report as a deviation, never silently.
"""
import numpy as np
import pandas as pd

# Coins alive on Hyperliquid without interruption from Jun 2024 to 2026-10-02
# (owner decision 2026-10-02). Key = symbol as in backtest/hyperliquid,
# value = Hyperliquid coin name. Order inside each group is the backtest order.
GROUP_L = {"ETH": "ETH", "XRP": "XRP", "SOL": "SOL", "DOGE": "DOGE", "TAO": "TAO",
           "MNT": "MNT", "SUI": "SUI", "SHIB": "kSHIB", "DOT": "DOT", "ENA": "ENA",
           "LINK": "LINK", "NEAR": "NEAR"}
GROUP_O = {"BNB": "BNB", "TRX": "TRX", "ADA": "ADA", "XLM": "XLM", "BCH": "BCH",
           "UNI": "UNI", "LTC": "LTC", "AVAX": "AVAX", "HBAR": "HBAR", "AAVE": "AAVE",
           "ONDO": "ONDO", "WLD": "WLD"}
ELIGIBLE = {**GROUP_L, **GROUP_O}
EXCLUDED = {"HYPE": "listing 2024-12", "ZEC": "listing 2025-04", "XMR": "listing 2025-08",
            "PUMP": "listing 2025-07", "CC": "listing 2025-10",
            "GRAM": "TON delisting 2026-06, relisting sebagai GRAM 2026-07"}

T_PASS = 2.0           # |t| needed for "lulus"
MIN_POSITIVE = 0.60    # B1: share of coins with expectancy > 0


def welch(a, b):
    """Difference of means a - b and Welch's t. (nan, nan) if a side is too small."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 2 or len(b) < 2:
        return np.nan, np.nan
    diff = a.mean() - b.mean()
    se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    return float(diff), float(diff / se) if se > 0 else np.nan


def t_stat(x):
    x = np.asarray(x, float)
    if len(x) < 2:
        return np.nan
    sd = x.std(ddof=1)
    return float(x.mean() / sd * np.sqrt(len(x))) if sd > 0 else np.nan


def monthly_t(trades: list, col: str = "net_R") -> tuple[float, int]:
    """t of the total R per calendar month of entry, and the number of months.

    Trades on different coins at the same time move together, so the
    trade-level t overstates the evidence. Summing per month is the cruder,
    more honest unit; it is reported next to the trade-level t, never instead.
    """
    if not trades:
        return np.nan, 0
    d = pd.DataFrame(trades)
    m = pd.to_datetime(d["entry_time"], utc=True).dt.strftime("%Y-%m")
    s = d.groupby(m)[col].sum()
    return t_stat(s.to_numpy()), len(s)


def monthly_diff_t(a: list, b: list, col: str = "net_R") -> tuple[float, int]:
    """t of (mean R of a - mean R of b) per entry month, months where both traded."""
    if not a or not b:
        return np.nan, 0
    def per_month(tr):
        d = pd.DataFrame(tr)
        m = pd.to_datetime(d["entry_time"], utc=True).dt.strftime("%Y-%m")
        return d.groupby(m)[col].mean()
    x = pd.concat([per_month(a), per_month(b)], axis=1, keys=["a", "b"]).dropna()
    return t_stat((x["a"] - x["b"]).to_numpy()), len(x)


def verdict_diff(diff: float, t: float) -> str:
    """Uji A / B2: is group 1 better than group 2?"""
    if not np.isfinite(diff) or diff <= 0:
        return "GAGAL"
    return "LULUS" if np.isfinite(t) and t >= T_PASS else "TIDAK JELAS"


def verdict_edge(exp_r: float, t: float, share_positive: float) -> str:
    """Uji B1: does the strategy have an edge at all in unseen data?"""
    if not np.isfinite(exp_r) or exp_r <= 0:
        return "GAGAL"
    ok = np.isfinite(t) and t >= T_PASS and share_positive >= MIN_POSITIVE
    return "LULUS" if ok else "TIDAK JELAS"


def in_window(trades: list, start, end, closed: bool) -> list:
    """Trades entered in [start, end). closed=True also requires exit < end.

    end=None means "to the end of the data". Selection windows use closed=True so
    a coin is never ranked on a trade whose outcome lies past the window.
    """
    start = pd.Timestamp(start)
    end = None if end is None else pd.Timestamp(end)
    out = []
    for t in trades:
        e = pd.Timestamp(t["entry_time"])
        if e < start or (end is not None and e >= end):
            continue
        if closed and end is not None and pd.Timestamp(t["exit_time"]) >= end:
            continue
        out.append(t)
    return out


def spearman(x, y) -> float:
    s = pd.concat([pd.Series(x, dtype=float), pd.Series(y, dtype=float)], axis=1).dropna()
    if len(s) < 3:
        return np.nan
    return float(s.iloc[:, 0].rank().corr(s.iloc[:, 1].rank()))


def fmt(x, nd=3, sign=True):
    if x is None or not np.isfinite(x):
        return "—"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"
