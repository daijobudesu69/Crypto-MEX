"""Pine-faithful indicators.

Pine seeds ta.ema and ta.rma with an SMA of the first `length` values and then
runs the recursion. ta.atr and ta.rsi are both RMA (Wilder) based -- a plain
rolling mean will not reproduce TradingView, which is why every smoother here is
written out explicitly rather than delegated to pandas.ewm.
"""
from . import compat  # noqa: F401
import numpy as np


def sma(x, n):
    x = np.asarray(x, dtype=float)
    out = np.full(x.shape, np.nan)
    if len(x) < n:
        return out
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def ema(x, n):
    """Pine ta.ema: seed = SMA(n) at index n-1, then alpha = 2/(n+1)."""
    x = np.asarray(x, dtype=float)
    out = np.full(x.shape, np.nan)
    if len(x) < n:
        return out
    a = 2.0 / (n + 1.0)
    out[n - 1] = np.mean(x[:n])
    for i in range(n, len(x)):
        out[i] = a * x[i] + (1.0 - a) * out[i - 1]
    return out


def rma(x, n):
    """Pine ta.rma (Wilder): seed = SMA(n) at index n-1, then alpha = 1/n."""
    x = np.asarray(x, dtype=float)
    out = np.full(x.shape, np.nan)
    if len(x) < n:
        return out
    a = 1.0 / n
    seed = x[:n]
    if np.isnan(seed).any():
        return out
    out[n - 1] = np.mean(seed)
    for i in range(n, len(x)):
        v = x[i]
        out[i] = a * (0.0 if np.isnan(v) else v) + (1.0 - a) * out[i - 1]
    return out


def true_range(high, low, close):
    """Pine ta.tr: first bar is high-low (no previous close)."""
    high = np.asarray(high, float); low = np.asarray(low, float); close = np.asarray(close, float)
    prev = np.roll(close, 1)
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    tr[0] = high[0] - low[0]
    return tr


def atr(high, low, close, n=14):
    return rma(true_range(high, low, close), n)


def rsi(close, n=14):
    """Pine ta.rsi: RMA of gains / RMA of losses."""
    close = np.asarray(close, float)
    d = np.diff(close, prepend=np.nan)
    up = np.where(np.isnan(d), np.nan, np.maximum(d, 0.0))
    dn = np.where(np.isnan(d), np.nan, np.maximum(-d, 0.0))
    up[0] = 0.0
    dn[0] = 0.0
    # Pine's rma over ta.change() starts accumulating from bar 1.
    ru = rma(up[1:], n)
    rd = rma(dn[1:], n)
    ru = np.insert(ru, 0, np.nan)
    rd = np.insert(rd, 0, np.nan)
    out = np.full(close.shape, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ru / rd
        out = 100.0 - 100.0 / (1.0 + rs)
    out = np.where(rd == 0, 100.0, out)
    out = np.where(ru == 0, 0.0, out)
    out[np.isnan(ru) | np.isnan(rd)] = np.nan
    return out


def highest(x, n):
    """Pine ta.highest(src, n): rolling max over the last n values inclusive."""
    x = np.asarray(x, float)
    out = np.full(x.shape, np.nan)
    for i in range(n - 1, len(x)):
        w = x[i - n + 1:i + 1]
        if np.isnan(w).any():
            continue
        out[i] = w.max()
    return out


def shift1(x):
    """Pine's `src[1]` -- previous bar's value."""
    x = np.asarray(x, float)
    out = np.empty_like(x)
    out[0] = np.nan
    out[1:] = x[:-1]
    return out
