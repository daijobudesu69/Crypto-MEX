"""Signal driver. Runs hourly; sends a message only when something happened.

Deliberately driven by bar state rather than by the clock. Each run asks "which
closed 4H bars have I not processed yet?" and works through them in order, so a
delayed or skipped GitHub Actions run catches up on the next one instead of
losing a signal. That is also why it is safe to run this more often than every
four hours: bars already processed are simply skipped, and no message repeats.
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402

from mex import datafeed, ledger, notify, sheets  # noqa: E402
from mex.config import load, ENGINE_VERSION  # noqa: E402
from mex.strategy import compute_features, step, pos_to_dict, pos_from_dict  # noqa: E402

STATE = "state/position.json"
RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")
SHA = os.environ.get("GITHUB_SHA", "")[:8]


def _delay_minutes(bar_ts):
    """Minutes between the bar closing and this message being built."""
    closed = pd.Timestamp(bar_ts) + pd.Timedelta("4h")
    return max(0.0, (pd.Timestamp.now(tz="UTC") - closed).total_seconds() / 60.0)


def main():
    cfg = load()
    p = cfg["params"]
    symbol = cfg["symbol"]
    run = {
        "run_at_utc": pd.Timestamp.now(tz="UTC").isoformat(), "status": "ok",
        "engine_version": ENGINE_VERSION, "run_id": RUN_ID, "commit_sha": SHA,
        "telegram_ok": "", "sheet_ok": "",
        "bars_processed": 0, "events_emitted": 0, "message": "",
    }

    hint = sheets.missing()
    if hint:
        print(f"[sheets] {hint}")

    try:
        feed = datafeed.fetch(limit=1000, prefer=cfg["prefer_source"])
    except Exception as e:  # noqa: BLE001
        run.update(status="data_error", message=f"{type(e).__name__}: {e}")
        ledger.log_run(run)
        notify.send(notify.alert_message("data feed gagal", e))
        print(traceback.format_exc())
        return 1

    df, source = feed.df, feed.source
    f = compute_features(df, p)
    ts = pd.DatetimeIndex(df["ts"])
    run.update(data_source=source, bars_available=len(df), last_bar_utc=ts[-1].isoformat())

    st = ledger.read_json(STATE, {})
    pos = pos_from_dict(st.get("position"))
    pending = st.get("pending")
    last_bar = st.get("last_bar")

    if last_bar is None:
        # First ever run: adopt the newest closed bar and stay flat. Replaying
        # history here would fire a burst of stale signals on day one.
        st = {"last_bar": ts[-1].isoformat(), "position": None, "pending": None,
              "started_at": pd.Timestamp.now(tz="UTC").isoformat(),
              "engine_version": ENGINE_VERSION}
        ledger.write_json(STATE, st)
        run.update(status="bootstrap", message=f"mulai dari bar {ts[-1]}",
                   position_open=False)
        ledger.log_run(run)
        print(f"[bootstrap] mulai dari {ts[-1]}, posisi kosong")
        return 0

    start = ts.searchsorted(pd.Timestamp(last_bar), side="right")
    todo = range(int(start), len(df))
    print(f"[run] sumber={source} bar tersedia={len(df)} terakhir diproses={last_bar} "
          f"-> {len(todo)} bar baru")

    sent_any = False
    for i in todo:
        pos, pending, events = step(f, ts, i, p, pos, pending)
        run["bars_processed"] += 1
        for ev in events:
            run["events_emitted"] += 1
            sent_any |= _handle(ev, symbol, source, p, ts)

    st.update(last_bar=ts[len(df) - 1].isoformat(), position=pos_to_dict(pos),
              pending=pending, engine_version=ENGINE_VERSION,
              updated_at=pd.Timestamp.now(tz="UTC").isoformat())
    ledger.write_json(STATE, st)

    run.update(position_open=pos is not None,
               position_side=("long" if pos and pos.side > 0 else
                              "short" if pos else ""),
               position_signal_id=pos.signal_id if pos else "",
               unrealised_R=(round((df["close"].iloc[-1] - pos.entry_price)
                                   * pos.side / pos.r_usdt, 3) if pos else ""),
               telegram_ok=sent_any if run["events_emitted"] else "no_events",
               sheet_ok=ledger.sheet_status())
    ledger.log_run(run)
    print(f"[run] selesai: {run['events_emitted']} event, posisi "
          f"{'TERBUKA' if pos else 'kosong'}")
    return 0


def _handle(ev, symbol, source, p, ts) -> bool:
    """Log an event and, when it is still actionable, notify. Returns sent?"""
    kind = ev["event"]
    bar = ev["bar"]
    ctx = ev.get("ctx", {})
    delay = _delay_minutes(bar)
    base = {
        "logged_at_utc": pd.Timestamp.now(tz="UTC").isoformat(), "event": kind,
        "bar_time_utc": bar.isoformat(), "symbol": symbol, "data_source": source,
        "engine_version": ENGINE_VERSION, "run_id": RUN_ID, "commit_sha": SHA,
        "signal_to_send_minutes": round(delay, 1), **ctx,
    }

    if kind == "SIGNAL":
        pd_ = ev["pending"]
        expired = pd.Timestamp.now(tz="UTC") > pd.Timestamp(pd_["expires_at"])
        ledger.log_event({
            **base, "signal_id": pd_["signal_id"],
            "side": "long" if pd_["side"] > 0 else "short",
            "ref_price": round(pd_["ref_price"], 4),
            "entry_zone_low": round(pd_["zone_low"], 4),
            "entry_zone_high": round(pd_["zone_high"], 4),
            "expires_at_utc": pd_["expires_at"], "r_usdt": round(pd_["r_est"], 4),
            "callback_pct": round(pd_["callback_pct_est"], 4),
            "stop_level": round(pd_["stop_est"], 4),
            "r_pct_of_price": round(pd_["callback_pct_est"], 4),
            "exit_reason": "EXPIRED_BEFORE_SEND" if expired else "",
        })
        if expired:
            # Catching up on an old bar: keep it in the ledger so the forward test
            # stays complete, but do not push a signal the user can no longer act
            # on -- and remember not to announce its entry or exit either.
            pd_["notified"] = False
            print(f"[skip] sinyal {pd_['signal_id']} sudah kedaluwarsa, tidak dikirim")
            return False
        pd_["notified"] = True
        return notify.send(notify.signal_message(pd_, ctx, symbol, source, delay))

    if kind == "ENTRY":
        pos = ev["pos"]
        ledger.log_event({
            **base, "signal_id": pos.signal_id,
            "side": "long" if pos.side > 0 else "short",
            "ref_price": round(ev["pending"]["ref_price"], 4),
            "entry_price": round(pos.entry_price, 4),
            "r_usdt": round(pos.r_usdt, 4), "callback_pct": round(pos.callback_pct, 4),
            "stop_level": round(pos.stop_initial, 4),
            "r_pct_of_price": round(pos.callback_pct, 4),
            "trail_at_event": round(pos.trail, 4),
        })
        if not pos.notified:
            return False
        return notify.send(notify.entry_message(pos, symbol, source))

    if kind == "EXIT":
        pos, px = ev["pos"], ev["exit_price"]
        ret = (px / pos.entry_price - 1) * 100 * pos.side
        t = {
            "signal_id": pos.signal_id, "symbol": symbol,
            "side": "long" if pos.side > 0 else "short", "data_source": source,
            "engine_version": ENGINE_VERSION,
            "signal_bar_utc": pos.signal_bar, "entry_bar_utc": pos.entry_bar,
            "exit_bar_utc": bar.isoformat(),
            "entry_price": round(pos.entry_price, 4), "exit_price": round(px, 4),
            "stop_initial": round(pos.stop_initial, 4),
            "final_trail": round(pos.trail, 4),
            "r_usdt": round(pos.r_usdt, 4), "callback_pct": round(pos.callback_pct, 4),
            "bars_held": pos.bars_held, "hours_held": pos.bars_held * 4,
            "ret_pct": round(ret, 4),
            "result_R": round((px - pos.entry_price) * pos.side / pos.r_usdt, 4),
            "mae_pct": round(pos.mae_pct, 4), "mfe_pct": round(pos.mfe_pct, 4),
            "mfe_R": round(pos.mfe_pct / 100 * pos.entry_price / pos.r_usdt, 4),
            "giveback_pct": round(pos.mfe_pct - ret, 4),
            "exit_reason": ev["reason"], "signal_to_send_minutes": round(delay, 1),
            "ref_price": round(pos.ref_price, 4) if pos.ref_price else "",
            # why this trade was taken, frozen at the signal bar
            "entry_atr14": pos.sig_ctx.get("atr14", ""),
            "entry_rsi": pos.sig_ctx.get("rsi", ""),
            "entry_rsi_roc": pos.sig_ctx.get("rsi_roc", ""),
            "entry_vol_ratio": pos.sig_ctx.get("vol_ratio", ""),
            "entry_ema_spread_pct": pos.sig_ctx.get("ema_spread_pct", ""),
            "entry_breakout_margin_pct": pos.sig_ctx.get("breakout_margin_pct", ""),
        }
        ledger.log_trade(t)
        ledger.log_event({
            **base, "signal_id": pos.signal_id, "side": t["side"],
            "entry_price": t["entry_price"], "exit_price": t["exit_price"],
            "bars_held": pos.bars_held, "trail_at_event": t["final_trail"],
            "exit_reason": ev["reason"], "r_usdt": t["r_usdt"],
            "callback_pct": t["callback_pct"],
        })
        if not pos.notified:
            return False
        return notify.send(notify.exit_message(t, symbol, source))

    return False


if __name__ == "__main__":
    sys.exit(main())
