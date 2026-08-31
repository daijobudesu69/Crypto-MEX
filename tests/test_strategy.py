"""Offline tests. No network, so they can gate every push.

The one that matters most is test_parity_with_backtest: it replays 4000 real
Binance perp bars and asserts the live signal arrays are bit-identical to the
ones produced by the backtest engine that passed T0-T14. Without it, this repo
could drift away from the strategy that was actually validated and nothing would
notice until the forward test had already produced meaningless data.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mex.compat  # noqa: F401,E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from mex.strategy import Params, compute_features, step  # noqa: E402
from mex import notify  # noqa: E402
from mex.indicators import rsi, atr, ema, sma, highest, shift1  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + detail}")


def load_fixture():
    df = pd.read_csv(os.path.join(FIX, "eth_4h_binance_perp.csv"))
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    with open(os.path.join(FIX, "expected_signals.json"), encoding="utf-8") as fh:
        exp = json.load(fh)
    return df, exp


# --------------------------------------------------------------------------- #
def test_indicators_against_wilder():
    """Wilder's own RSI worked example, computed by hand.

    14 changes: gains sum 3.34, losses sum 1.40 -> avg 0.238571 / 0.100000
    RS = 2.385714 -> RSI = 100 - 100/3.385714 = 70.4639
    """
    c = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
         45.89, 46.03, 45.61, 46.28, 46.28, 46.00]
    r = rsi(c, 14)
    check("RSI cocok dengan hitungan tangan Wilder (bar 14)",
          abs(r[14] - 70.4639) < 1e-3, f"dapat {r[14]:.4f}")
    check("RSI cocok dengan hitungan tangan Wilder (bar 15)",
          abs(r[15] - 66.2497) < 1e-3, f"dapat {r[15]:.4f}")

    # ATR on a constant-range series must equal that range exactly.
    n = 40
    hi = np.arange(n, dtype=float) + 100.0
    lo = hi - 2.0
    cl = hi - 1.0
    a = atr(hi, lo, cl, 14)
    check("ATR pada rentang konstan = rentang itu sendiri",
          abs(a[-1] - 2.0) < 1e-9, f"dapat {a[-1]:.6f}")

    x = np.arange(50, dtype=float)
    check("EMA seed = SMA(n) persis seperti Pine",
          abs(ema(x, 20)[19] - x[:20].mean()) < 1e-12)
    check("SMA benar", abs(sma(x, 10)[9] - x[:10].mean()) < 1e-12)
    check("highest(shift1(x), n) tidak menyentuh bar berjalan",
          highest(shift1(x), 5)[10] == x[9])


def test_parity_with_backtest():
    df, exp = load_fixture()
    f = compute_features(df, Params())
    got_L = np.where(f["long_signal"])[0].tolist()
    got_S = np.where(f["fade_signal"])[0].tolist()
    check(f"sinyal LONG identik dengan engine backtest ({len(exp['long_idx'])} sinyal)",
          got_L == exp["long_idx"],
          f"engine {len(exp['long_idx'])} vs live {len(got_L)}; "
          f"beda {sorted(set(got_L) ^ set(exp['long_idx']))[:8]}")
    check(f"sinyal FADE SHORT identik dengan engine backtest ({len(exp['fade_idx'])} sinyal)",
          got_S == exp["fade_idx"],
          f"engine {len(exp['fade_idx'])} vs live {len(got_S)}")


def test_no_lookahead_and_ratchet():
    """The trail must never move against the position, and the bar that triggers
    the stop must be tested against the level from the PREVIOUS bar's close."""
    df, _ = load_fixture()
    p = Params()
    f = compute_features(df, p)
    ts = pd.DatetimeIndex(df["ts"])

    pos = pending = None
    trails, breaches, n_entries, n_exits = [], 0, 0, 0
    for i in range(len(df)):
        prev_trail = pos.trail if pos else None
        prev_side = pos.side if pos else None
        pos, pending, events = step(f, ts, i, p, pos, pending)
        for ev in events:
            n_entries += ev["event"] == "ENTRY"
            n_exits += ev["event"] == "EXIT"
            if ev["event"] == "EXIT":
                # the exit must be justified by the level carried IN, not the
                # one recomputed on this same bar
                lo, hi = float(f["low"][i]), float(f["high"][i])
                ok = (lo <= prev_trail) if prev_side > 0 else (hi >= prev_trail)
                if not ok:
                    breaches += 1
        if pos and prev_trail is not None and pos.side == prev_side:
            moved = (pos.trail - prev_trail) * pos.side
            if moved < -1e-9:
                trails.append(moved)

    check("trailing stop tidak pernah bergerak melawan posisi (ratchet)",
          not trails, f"{len(trails)} kali mundur, terburuk {min(trails) if trails else 0:.6f}")
    check("exit selalu dibenarkan oleh trail bar SEBELUMNYA (bebas lookahead)",
          breaches == 0, f"{breaches} exit tanpa dasar")
    check("state machine menghasilkan entry dan exit yang berpasangan",
          abs(n_entries - n_exits) <= 1, f"{n_entries} entry vs {n_exits} exit")
    print(f"        ({n_entries} entry, {n_exits} exit di {len(df)} bar fixture)")


def test_entry_bar_has_no_stop_test():
    """The stop level depends on the fill bar's own ATR, so it cannot rest at the
    exchange until that bar has closed. Exiting on the entry bar would be fiction."""
    df, _ = load_fixture()
    p = Params()
    f = compute_features(df, p)
    ts = pd.DatetimeIndex(df["ts"])
    pos = pending = None
    bad = 0
    for i in range(len(df)):
        pos, pending, events = step(f, ts, i, p, pos, pending)
        kinds = [e["event"] for e in events]
        if "ENTRY" in kinds and "EXIT" in kinds:
            bad += 1
    check("tidak ada exit di bar entry yang sama", bad == 0, f"{bad} kasus")


def test_r_and_callback_consistency():
    """1R, the callback rate and the initial stop must be the same number seen
    three ways -- this is the invariant the whole manual-trading setup rests on."""
    df, _ = load_fixture()
    p = Params()
    f = compute_features(df, p)
    ts = pd.DatetimeIndex(df["ts"])
    pos = pending = None
    worst = 0.0
    n = 0
    for i in range(len(df)):
        pos, pending, events = step(f, ts, i, p, pos, pending)
        for ev in events:
            if ev["event"] == "ENTRY":
                q = ev["pos"]
                n += 1
                worst = max(worst, abs(q.callback_pct - q.r_usdt / q.entry_price * 100))
                worst = max(worst, abs(abs(q.entry_price - q.stop_initial) - q.r_usdt))
    check(f"callback% == 1R/harga dan stop awal == entry -/+ 1R ({n} entry)",
          worst < 1e-9, f"selisih terburuk {worst:.3e}")


def test_messages_render():
    """Render every template on a real trade.

    The templates are f-strings that only execute when a signal actually fires,
    so a syntax or formatting error in them would otherwise stay invisible until
    the first live signal -- exactly the moment it must not fail.
    """
    df, _ = load_fixture()
    p = Params()
    f = compute_features(df, p)
    ts = pd.DatetimeIndex(df["ts"])
    pos = pending = None
    seen = {}
    for i in range(len(df)):
        pos, pending, events = step(f, ts, i, p, pos, pending)
        for ev in events:
            if ev["event"] == "SIGNAL":
                seen["SIGNAL"] = notify.signal_message(
                    ev["pending"], ev["ctx"], "ETHUSDT", "test", 60.0)
            elif ev["event"] == "ENTRY":
                seen["ENTRY"] = notify.entry_message(ev["pos"], "ETHUSDT", "test")
            elif ev["event"] == "EXIT":
                q, px = ev["pos"], ev["exit_price"]
                ret = (px / q.entry_price - 1) * 100 * q.side
                seen["EXIT"] = notify.exit_message({
                    "signal_id": q.signal_id, "side": "long" if q.side > 0 else "short",
                    "exit_bar_utc": ev["bar"].isoformat(), "entry_price": q.entry_price,
                    "exit_price": px, "ret_pct": ret, "bars_held": q.bars_held,
                    "result_R": (px - q.entry_price) * q.side / q.r_usdt,
                    "hours_held": q.bars_held * 4, "mfe_pct": q.mfe_pct,
                    "mae_pct": q.mae_pct, "giveback_pct": q.mfe_pct - ret,
                }, "ETHUSDT", "test")
    for kind in ("SIGNAL", "ENTRY", "EXIT"):
        check(f"pesan {kind} ter-render", kind in seen and len(seen[kind]) > 100)
    check("heartbeat ter-render", len(notify.heartbeat_message({
        "now": "2026-08-31T00:00:00", "last_bar": "2026-08-31T00:00:00",
        "source": "test", "position": None, "data_ok": True,
        "signals_30d": 0, "trades_30d": 0, "trades_total": 0, "sum_R": 0.0})) > 100)
    check("pesan peringatan ter-render", len(notify.alert_message("uji", "detail")) > 20)
    # A formula shown to the user must reproduce the number printed beside it.
    msg = seen.get("SIGNAL", "")
    check("pesan sinyal memuat rumus Trailing Stop", "× ATR ÷ price × 100" in msg)
    check("pesan sinyal tidak menyisakan placeholder", "{" not in msg and "}" not in msg)


def test_expiry_zone():
    p = Params()
    check("zona entry = +/- 0.5R sesuai kesepakatan", p.entry_zone_r == 0.5)
    check("sinyal hangus setelah 8 jam", p.expiry_hours == 8.0)
    check("parameter strategi sama dengan yang dibacktest",
          (p.n_lookback, p.vol_mult, p.ema_fast, p.ema_slow, p.rsi_len, p.roc_len,
           p.rsi_confirm, p.atr_len, p.atr_sl_mult, p.allow_shorts)
          == (20, 1.5, 20, 50, 14, 5, 55.0, 14, 1.5, True))


if __name__ == "__main__":
    print("test_strategy.py")
    for t in (test_indicators_against_wilder, test_parity_with_backtest,
              test_no_lookahead_and_ratchet, test_entry_bar_has_no_stop_test,
              test_r_and_callback_consistency, test_messages_render,
              test_expiry_zone):
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    sys.exit(1 if FAIL else 0)
