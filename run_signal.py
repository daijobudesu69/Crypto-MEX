"""Signal driver. Runs hourly; sends a message only when something happened.

Deliberately driven by bar state rather than by the clock. Each run asks "which
closed 4H bars have I not processed yet?" and works through them in order, so a
delayed or skipped GitHub Actions run catches up on the next one instead of
losing a signal. That is also why it is safe to run this more often than every
four hours: bars already processed are simply skipped, and no message repeats.

Delivery is queue-then-flush, not send-and-hope. Every message is written into
state["outbox"] first and only removed once Telegram has accepted it, and every
delivered message leaves its key in state["sent_ids"]. Those two lists are what
make the run idempotent:

  * A send that fails leaves the message in the outbox, so the next run retries
    it. Previously a single 25-second timeout lost the signal permanently while
    the job still reported success.
  * A run that dies after sending but before its state commit is replayed on the
    next run -- and the sent_ids check means the replay does not send twice.

last_bar always advances past every bar that step() has seen, even when delivery
failed. It has to: step() is a state machine and re-feeding it a bar it has
already processed would corrupt the trailing stop. Delivery is tracked
separately precisely so it can be retried without replaying the strategy.
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

# How long a delivered-late confirmation is still worth reading. Signals carry
# their own expiry from the strategy (expiry_hours); ENTRY and EXIT are records
# rather than instructions, so they get a flat day before they are dropped.
CONFIRM_TTL = pd.Timedelta("24h")
# Enough history to recognise a replayed bar; short enough to keep state small.
SENT_IDS_KEPT = 300
# With MEX_QUIET_IDLE=1 (the polling watcher) a run that saw no new bar and
# emitted no event only writes its runs.csv row once this much time has passed.
IDLE_LOG_EVERY = pd.Timedelta(os.environ.get("MEX_IDLE_LOG_EVERY", "60min"))


def _should_log_run(run) -> bool:
    """Always log a run that did something or went wrong; rate-limit the rest.

    The watcher polls every 10 minutes, so logging unconditionally would add 144
    near-identical rows -- and 144 commits -- per day. Anything that processed a
    bar, emitted an event or failed is always recorded, so nothing that matters
    to the forward test is ever suppressed.
    """
    if run["bars_processed"] or run["events_emitted"] or run["status"] != "ok":
        return True
    if os.environ.get("MEX_QUIET_IDLE") != "1":
        return True
    last = ledger.last_run_at()
    if not last:
        return True
    try:
        return pd.Timestamp.now(tz="UTC") - pd.Timestamp(last) >= IDLE_LOG_EVERY
    except Exception:  # noqa: BLE001
        return True


def _delay_minutes(bar_ts):
    """Minutes between the bar closing and this message being built."""
    closed = pd.Timestamp(bar_ts) + datafeed.BAR
    return max(0.0, (pd.Timestamp.now(tz="UTC") - closed).total_seconds() / 60.0)


def _flush(st) -> tuple[int, int, int]:
    """Try to deliver everything queued. Returns (sent, failed, dropped).

    Runs before new bars are processed so the oldest message goes out first, and
    again afterwards for anything queued by this run.
    """
    now = pd.Timestamp.now(tz="UTC")
    sent_ids = st.setdefault("sent_ids", [])
    keep, sent, failed, dropped = [], 0, 0, 0

    for m in st.get("outbox", []):
        if m["key"] in sent_ids:
            continue                      # already delivered on an earlier run
        if now > pd.Timestamp(m["expires_at"]):
            dropped += 1
            print(f"[outbox] {m['key']} hangus sebelum sempat terkirim, dibuang")
            continue
        # Without a bot token the documented behaviour is to print and carry on,
        # so the pipeline can be exercised before the bot exists. Queuing here
        # instead would make every run red forever.
        if not notify.configured():
            notify.send(m["text"])
            sent_ids.append(m["key"])
            sent += 1
            continue
        if notify.send(m["text"]):
            sent_ids.append(m["key"])
            sent += 1
        else:
            m["attempts"] = m.get("attempts", 0) + 1
            keep.append(m)
            failed += 1
            print(f"[outbox] {m['key']} gagal terkirim "
                  f"(percobaan ke-{m['attempts']}), akan dicoba lagi")

    st["outbox"] = keep
    del sent_ids[:-SENT_IDS_KEPT]
    return sent, failed, dropped


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

    # A corrupt state file must never be mistaken for a first run: bootstrapping
    # on top of one would silently abandon an open position and its stop.
    try:
        st = ledger.read_json(STATE, {})
    except ledger.StateCorrupt as e:
        run.update(status="state_error", message=str(e))
        ledger.log_run(run)
        notify.send(notify.alert_message("state rusak", e))
        print(traceback.format_exc())
        return 1

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

    pos = pos_from_dict(st.get("position"))
    pending = st.get("pending")
    last_bar = st.get("last_bar")

    if last_bar is None:
        # First ever run: adopt the newest closed bar and stay flat. Replaying
        # history here would fire a burst of stale signals on day one.
        st = {"last_bar": ts[-1].isoformat(), "position": None, "pending": None,
              "sent_ids": [], "outbox": [],
              "started_at": pd.Timestamp.now(tz="UTC").isoformat(),
              "engine_version": ENGINE_VERSION}
        ledger.write_json(STATE, st)
        run.update(status="bootstrap", message=f"mulai dari bar {ts[-1]}",
                   position_open=False)
        ledger.log_run(run)
        print(f"[bootstrap] mulai dari {ts[-1]}, posisi kosong")
        return 0

    # Anything left over from a previous run goes out before this run's own work.
    sent, failed, dropped = _flush(st)

    start = ts.searchsorted(pd.Timestamp(last_bar), side="right")
    todo = range(int(start), len(df))
    print(f"[run] sumber={source} bar tersedia={len(df)} terakhir diproses={last_bar} "
          f"-> {len(todo)} bar baru")

    queued = []
    for i in todo:
        pos, pending, events = step(f, ts, i, p, pos, pending)
        run["bars_processed"] += 1
        for ev in events:
            run["events_emitted"] += 1
            queued += _handle(ev, symbol, source, p, st)

    st.setdefault("outbox", []).extend(queued)
    s2, f2, d2 = _flush(st)
    sent, failed, dropped = sent + s2, f2, dropped + d2

    st.update(last_bar=ts[len(df) - 1].isoformat(), position=pos_to_dict(pos),
              pending=pending, engine_version=ENGINE_VERSION,
              updated_at=pd.Timestamp.now(tz="UTC").isoformat())
    ledger.write_json(STATE, st)

    if failed:
        telegram = f"failed_{failed}" if not sent else f"partial_{sent}/{sent + failed}"
    elif sent:
        telegram = "sent"
    else:
        telegram = "nothing_to_send"

    run.update(position_open=pos is not None,
               position_side=("long" if pos and pos.side > 0 else
                              "short" if pos else ""),
               position_signal_id=pos.signal_id if pos else "",
               unrealised_R=(round((df["close"].iloc[-1] - pos.entry_price)
                                   * pos.side / pos.r_usdt, 3) if pos else ""),
               telegram_ok=telegram,
               sheet_ok=ledger.sheet_status())
    if failed:
        run["status"] = "delivery_error"
        run["message"] = f"{failed} pesan masih di outbox"
    if _should_log_run(run):
        ledger.log_run(run)
    else:
        print("[run] idle, baris runs.csv ditahan (MEX_QUIET_IDLE)")
    print(f"[run] selesai: {run['events_emitted']} event, posisi "
          f"{'TERBUKA' if pos else 'kosong'}, kirim={telegram}"
          + (f", dibuang={dropped}" if dropped else ""))

    # Exit non-zero so GitHub reports the failure immediately. The message stays
    # in the outbox either way, so the next run retries it regardless.
    return 1 if failed else 0


def _handle(ev, symbol, source, p, st) -> list:
    """Log an event and return the messages it should queue (0 or 1)."""
    kind = ev["event"]
    bar = ev["bar"]
    ctx = ev.get("ctx", {})
    delay = _delay_minutes(bar)
    now = pd.Timestamp.now(tz="UTC")
    base = {
        "logged_at_utc": now.isoformat(), "event": kind,
        "bar_time_utc": bar.isoformat(), "symbol": symbol, "data_source": source,
        "engine_version": ENGINE_VERSION, "run_id": RUN_ID, "commit_sha": SHA,
        "signal_to_send_minutes": round(delay, 1), **ctx,
    }

    def msg(signal_id, text, expires_at):
        key = f"{kind}:{signal_id}"
        if key in st.get("sent_ids", []):
            print(f"[dedup] {key} sudah pernah terkirim, tidak diulang")
            return []
        return [{"key": key, "kind": kind, "signal_id": signal_id, "text": text,
                 "expires_at": str(expires_at), "queued_at": now.isoformat(),
                 "attempts": 0}]

    if kind == "SIGNAL":
        pd_ = ev["pending"]
        expired = now > pd.Timestamp(pd_["expires_at"])
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
            return []
        pd_["notified"] = True
        return msg(pd_["signal_id"],
                   notify.signal_message(pd_, ctx, symbol, source, delay,
                                         atr_mult=p.atr_sl_mult),
                   pd_["expires_at"])

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
        # Announced only when the signal itself was announced. Confirming an entry
        # for a signal the user never saw would be unreadable.
        if not pos.notified:
            return []
        return msg(pos.signal_id, notify.entry_message(pos, symbol, source),
                   now + CONFIRM_TTL)

    if kind == "EXIT":
        pos, px = ev["pos"], ev["exit_price"]
        ret = (px / pos.entry_price - 1) * 100 * pos.side
        hours = pos.bars_held * datafeed.BAR.total_seconds() / 3600.0
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
            "bars_held": pos.bars_held, "hours_held": hours,
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
            return []
        return msg(pos.signal_id, notify.exit_message(t, symbol, source),
                   now + CONFIRM_TTL)

    return []


if __name__ == "__main__":
    sys.exit(main())
