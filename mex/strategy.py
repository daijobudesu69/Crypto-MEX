"""MEX signal rules and the live trailing-stop state machine.

compute_features() is a verbatim port of the function in the validated backtest
engine; tests/test_parity.py asserts it produces bit-identical signal arrays on
the same data, so this file cannot silently drift from what was tested.

The per-bar ordering in step() is the same one the backtest unit-tests pin down:
the stop tested against bar t's low is the level computed at the close of bar
t-1. Update the trail first and you are testing a bar against information from
inside that same bar, which is lookahead and inflates every result.

Exit model is Mode B (a stop order resting at the exchange), and the trail is the
frozen-callback variant agreed for manual trading: the callback rate is fixed at
entry to 1.5 x ATR / entry_price, so one Binance Trailing Stop order placed once
reproduces it without any further intervention.
"""
from . import compat  # noqa: F401
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd

from .indicators import ema, sma, rsi, atr, highest, shift1


@dataclass
class Params:
    n_lookback: int = 20
    vol_len: int = 20
    vol_mult: float = 1.5
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_len: int = 14
    roc_len: int = 5
    rsi_confirm: float = 55.0
    atr_len: int = 14
    atr_sl_mult: float = 1.5
    risk_pct: float = 1.0
    allow_shorts: bool = True
    entry_zone_r: float = 0.5      # signal stays valid within +/- this many R
    expiry_hours: float = 8.0      # ... and only this long after the signal bar


def compute_features(df: pd.DataFrame, p: Params) -> dict:
    """Indicators + raw entry conditions, evaluated on closed bars only."""
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    v = df["volume"].to_numpy(float)

    f = {}
    f["open"], f["high"], f["low"], f["close"], f["volume"] = o, h, l, c, v
    f["ema_fast"] = ema(c, p.ema_fast)
    f["ema_slow"] = ema(c, p.ema_slow)
    f["rsi"] = rsi(c, p.rsi_len)
    f["rsi_roc"] = f["rsi"] - np.concatenate(
        [np.full(p.roc_len, np.nan), f["rsi"][:-p.roc_len]]
    )
    f["atr"] = atr(h, l, c, p.atr_len)
    f["vol_avg"] = sma(v, p.vol_len)
    # Pine: ta.highest(high[1], n) -- the previous n bars, current bar excluded.
    f["prior_high"] = highest(shift1(h), p.n_lookback)
    f["prior_rsi_peak"] = highest(shift1(f["rsi"]), p.n_lookback)

    uptrend = f["ema_fast"] > f["ema_slow"]
    breakout_up = (h > f["prior_high"]) & (v > f["vol_avg"] * p.vol_mult)
    f["breakout_up"] = breakout_up
    f["long_signal"] = (
        breakout_up & uptrend & (f["rsi_roc"] > 0) & (f["rsi"] > p.rsi_confirm)
    )
    if p.allow_shorts:
        f["fade_signal"] = (
            breakout_up
            & (f["ema_fast"] < f["ema_slow"])
            & ((f["rsi"] < f["prior_rsi_peak"]) | (f["rsi_roc"] < 0))
        )
    else:
        f["fade_signal"] = np.zeros(len(c), bool)

    valid = ~(
        np.isnan(f["ema_slow"]) | np.isnan(f["atr"]) | np.isnan(f["prior_high"])
        | np.isnan(f["vol_avg"]) | np.isnan(f["rsi_roc"])
    )
    f["long_signal"] = f["long_signal"] & valid
    f["fade_signal"] = f["fade_signal"] & valid
    return f


def context(f: dict, i: int) -> dict:
    """Indicator snapshot at bar i, carried onto every logged row for later analysis."""
    def g(k):
        v = f[k][i]
        return None if not np.isfinite(v) else round(float(v), 6)
    vol_ratio = (f["volume"][i] / f["vol_avg"][i]) if np.isfinite(f["vol_avg"][i]) and f["vol_avg"][i] else None
    brk = ((f["high"][i] / f["prior_high"][i] - 1.0) * 100.0) if np.isfinite(f["prior_high"][i]) else None
    return {
        "open": g("open"), "high": g("high"), "low": g("low"), "close": g("close"),
        "volume": g("volume"), "atr14": g("atr"), "rsi": g("rsi"), "rsi_roc": g("rsi_roc"),
        "ema_fast": g("ema_fast"), "ema_slow": g("ema_slow"),
        "ema_spread_pct": (round((f["ema_fast"][i] / f["ema_slow"][i] - 1) * 100, 6)
                           if np.isfinite(f["ema_slow"][i]) else None),
        "vol_avg": g("vol_avg"), "vol_ratio": None if vol_ratio is None else round(vol_ratio, 4),
        "prior_high": g("prior_high"), "prior_rsi_peak": g("prior_rsi_peak"),
        "breakout_margin_pct": None if brk is None else round(brk, 4),
        "atr_pct_of_price": (round(f["atr"][i] / f["close"][i] * 100, 4)
                             if np.isfinite(f["atr"][i]) else None),
    }


# --------------------------------------------------------------------------- #
# live state machine
# --------------------------------------------------------------------------- #
@dataclass
class Position:
    side: int                    # +1 long, -1 short
    signal_id: str
    signal_bar: str              # bar whose close produced the signal
    entry_bar: str               # bar whose open we treat as the reference fill
    entry_price: float
    r_usdt: float                # 1R = 1.5 x ATR at the entry bar
    callback_pct: float          # frozen trailing callback = r_usdt / entry_price
    stop_initial: float
    trail: float                 # level carried INTO the next bar
    hi_water: float
    lo_water: float
    mae_pct: float = 0.0
    mfe_pct: float = 0.0
    bars_held: int = 0
    max_trail: float = 0.0
    notified: bool = True   # False when the signal expired before we could send it
    ref_price: float = 0.0  # close of the signal bar -- what the alert quoted
    # Indicator snapshot at the signal bar. Kept on the position so the closed
    # trade can be attributed later; none of it is recoverable after the fact.
    sig_ctx: dict = field(default_factory=dict)


def _sid(ts: pd.Timestamp, side: int) -> str:
    return f"{ts:%Y%m%dT%H%M}-{'L' if side > 0 else 'S'}"


def step(f: dict, ts: pd.DatetimeIndex, i: int, p: Params,
         pos: Optional[Position], pending: Optional[dict]):
    """Process one closed bar. Returns (position, pending_entry, events)."""
    events = []
    o, h, l, c = f["open"], f["high"], f["low"], f["close"]
    bar = ts[i]

    # --- A. a signal from the previous bar becomes the reference entry at this open
    just_entered = False
    if pending is not None and pos is None:
        side = int(pending["side"])
        a = f["atr"][i]
        if np.isfinite(a) and a > 0:
            entry = float(o[i])
            r = float(a * p.atr_sl_mult)          # 1R uses the ATR of the FILL bar
            pos = Position(
                side=side, signal_id=pending["signal_id"],
                signal_bar=pending["signal_bar"], entry_bar=bar.isoformat(),
                entry_price=entry, r_usdt=r, callback_pct=r / entry * 100.0,
                stop_initial=entry - side * r, trail=entry - side * r,
                hi_water=float(h[i]), lo_water=float(l[i]), max_trail=entry - side * r,
                notified=bool(pending.get("notified", True)),
                ref_price=float(pending.get("ref_price", 0.0)),
                sig_ctx=dict(pending.get("sig_ctx", {})),
            )
            just_entered = True
            events.append({"event": "ENTRY", "bar": bar, "pos": pos,
                           "ctx": context(f, i), "pending": pending})
        pending = None

    # --- B. fill test against the trail carried in from the previous bar's close
    if pos is not None and not just_entered:
        hit = (pos.side > 0 and l[i] <= pos.trail) or (pos.side < 0 and h[i] >= pos.trail)
        if hit:
            # a resting stop fills at its level, or at the open if price gapped past it
            px = (min(pos.trail, float(o[i])) if pos.side > 0
                  else max(pos.trail, float(o[i])))
            pos.bars_held = i - int(ts.get_loc(pd.Timestamp(pos.entry_bar)))
            events.append({"event": "EXIT", "bar": bar, "pos": pos, "exit_price": px,
                           "ctx": context(f, i), "reason": "trailing_stop"})
            pos = None

    # --- C/D. water-mark, then the ratchet. Never moves against the position.
    if pos is not None:
        if pos.side > 0:
            pos.hi_water = max(pos.hi_water, float(h[i]))
            pos.trail = max(pos.trail, pos.hi_water * (1.0 - pos.callback_pct / 100.0))
            pos.mae_pct = min(pos.mae_pct, (float(l[i]) / pos.entry_price - 1) * 100)
            pos.mfe_pct = max(pos.mfe_pct, (float(h[i]) / pos.entry_price - 1) * 100)
        else:
            pos.lo_water = min(pos.lo_water, float(l[i]))
            pos.trail = min(pos.trail, pos.lo_water * (1.0 + pos.callback_pct / 100.0))
            pos.mae_pct = min(pos.mae_pct, -(float(h[i]) / pos.entry_price - 1) * 100)
            pos.mfe_pct = max(pos.mfe_pct, -(float(l[i]) / pos.entry_price - 1) * 100)
        pos.max_trail = pos.trail
        pos.bars_held = i - int(ts.get_loc(pd.Timestamp(pos.entry_bar)))

    # --- E. signal evaluation at this bar's close
    if pos is None and pending is None:
        side = 1 if f["long_signal"][i] else (-1 if f["fade_signal"][i] else 0)
        if side != 0 and np.isfinite(f["atr"][i]) and f["atr"][i] > 0:
            ref = float(c[i])
            r_est = float(f["atr"][i] * p.atr_sl_mult)
            pending = {
                "side": side, "signal_id": _sid(bar, side), "signal_bar": bar.isoformat(),
                "ref_price": ref, "r_est": r_est,
                "callback_pct_est": r_est / ref * 100.0,
                "zone_low": ref - p.entry_zone_r * r_est,
                "zone_high": ref + p.entry_zone_r * r_est,
                "stop_est": ref - side * r_est,
                "expires_at": (bar + pd.Timedelta(hours=p.expiry_hours)).isoformat(),
                "sig_ctx": context(f, i),
            }
            events.append({"event": "SIGNAL", "bar": bar, "pending": pending,
                           "ctx": context(f, i)})

    return pos, pending, events


def pos_to_dict(pos: Optional[Position]):
    return None if pos is None else asdict(pos)


def pos_from_dict(d) -> Optional[Position]:
    return None if not d else Position(**d)
