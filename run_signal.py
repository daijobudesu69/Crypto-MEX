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
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402

from mex import datafeed, ledger, notify, sheets, state  # noqa: E402
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


def _fingerprint(state) -> str:
    """State's content, ignoring the timestamp that changes on every run.

    `updated_at` alone used to make position.json differ every single time, so
    the watcher committed every 10 minutes even when nothing had happened --
    144 commits a day of pure noise, which is exactly what MEX_QUIET_IDLE was
    supposed to prevent for runs.csv. Comparing on content instead means the
    file is only rewritten when something real changed, and `updated_at` then
    honestly means "when the state last changed".
    """
    return json.dumps({k: v for k, v in state.items() if k != "updated_at"},
                      sort_keys=True, default=str)


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


# Appended at SEND time, not at build time, and only to a message that
# actually sat in the outbox. In normal operation the queue is flushed in the
# same run that fills it, so this never appears.
QUEUE_NOTE = (
    "\n\n⏳ <b>Tertahan {mins:.0f} menit di antrean kirim</b> "
    "— harga sudah bergerak sejak pesan ini dibuat, cek ulang zona "
    "entry sebelum bertindak.")


def _queued_minutes(m) -> float:
    """How long this message has been waiting, measured when it is SENT.

    signal_message() prints its own lateness, but that number is frozen when the
    message is BUILT. A message that then sat in the outbox through a Telegram
    outage arrived hours later still advertising the delay it had at queue time,
    which is the one number in it a reader acts on.
    """
    q = m.get("queued_at")
    if not q:
        return 0.0
    try:
        return max(0.0, (pd.Timestamp.now(tz="UTC")
                         - pd.Timestamp(q)).total_seconds() / 60.0)
    except Exception:  # noqa: BLE001
        return 0.0


def _mark_unnotified(st, m) -> None:
    """A SIGNAL dropped from the outbox was never seen -- silence its ENTRY/EXIT.

    README promises an expired signal is recorded but not announced, and
    _handle() honours that for a signal that was already stale when it was
    built. A signal queued in time that then expired waiting out a Telegram
    outage took the other path: the drop below discarded it while
    pending["notified"] stayed True, so the user's next message was a bare
    "ENTRY TERCATAT" for a signal they never received. By the time the 8-hour
    expiry lands the pending has usually already become a position, so both
    slots are checked.
    """
    sl = (st.get("symbols") or {}).get(m.get("symbol"))
    if not sl:
        return
    for name in ("pending", "position"):
        held = sl.get(name)
        if held and held.get("signal_id") == m.get("signal_id"):
            held["notified"] = False
            print(f"[outbox] {m['key']}: {name} ditandai notified=False, "
                  f"entry/exit-nya tidak akan diumumkan")


def _flush(st) -> tuple[int, int, int]:
    """Try to deliver everything queued. Returns (sent, failed, dropped).

    Runs before new bars are processed so the oldest message goes out first, and
    again afterwards for anything queued by this run.
    """
    now = pd.Timestamp.now(tz="UTC")
    sent_ids = st.setdefault("sent_ids", [])
    keep, sent, failed, dropped = [], 0, 0, 0
    # Signals dropped unsent during THIS pass. Clearing notified only stops
    # future announcements; an ENTRY queued before the outage began is already
    # sitting in this list with a 24-hour TTL of its own, and would still be
    # delivered -- a bare "ENTRY TERCATAT" for a signal the user never saw,
    # which is the whole symptom. The outbox is chronological, so a single
    # forward pass sees the SIGNAL before its own ENTRY and EXIT.
    orphaned = set()

    for m in st.get("outbox", []):
        if m["key"] in sent_ids:
            continue                      # already delivered on an earlier run
        if now > pd.Timestamp(m["expires_at"]):
            dropped += 1
            if m.get("kind") == "SIGNAL":
                _mark_unnotified(st, m)
                orphaned.add((m.get("symbol"), m.get("signal_id")))
            print(f"[outbox] {m['key']} hangus sebelum sempat terkirim, dibuang")
            continue
        if (m.get("symbol"), m.get("signal_id")) in orphaned:
            dropped += 1
            print(f"[outbox] {m['key']} dibuang: sinyalnya sendiri hangus tanpa "
                  f"pernah terkirim, konfirmasi ini tidak akan terbaca")
            continue
        text = m["text"]
        waited = _queued_minutes(m)
        if m.get("kind") == "SIGNAL" and waited > 15:
            text += QUEUE_NOTE.format(mins=waited)
        # Without a bot token the documented behaviour is to print and carry on,
        # so the pipeline can be exercised before the bot exists. Queuing here
        # instead would make every run red forever.
        if not notify.configured():
            notify.send(text)
            sent_ids.append(m["key"])
            sent += 1
            continue
        if notify.send(text):
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


def _short(sym: str) -> str:
    return sym.replace("USDT", "")


def _process(sym, cfg, p, st, run, queued) -> dict | None:
    """Advance one symbol's state machine. Returns a summary, or None on failure.

    Each symbol keeps its own last_bar, position and pending, so one symbol
    falling behind -- or its feed being down -- cannot move another symbol's
    state machine. Failures are reported to the caller rather than raised: with
    four feeds, one being briefly unreachable must not cost the other three
    their bars.
    """
    try:
        feed = datafeed.fetch(limit=1000, prefer=cfg["prefer_source"], symbol=sym)
    except Exception as e:  # noqa: BLE001
        print(f"[data] {sym} GAGAL: {type(e).__name__}: {e}")
        return None

    df, source = feed.df, feed.source
    f = compute_features(df, p)
    ts = pd.DatetimeIndex(df["ts"])
    sl = state.slot(st, sym)
    info = {"source": source, "bars_available": len(df),
            "last_bar": ts[-1].isoformat(), "last_close": float(df["close"].iloc[-1]),
            "pos": None, "bars": 0}

    if sl.get("last_bar") is None:
        # First run for this symbol: adopt the newest closed bar and stay flat.
        # Replaying history here would fire a burst of stale signals on day one.
        sl.update(last_bar=ts[-1].isoformat(), position=None, pending=None)
        info["bootstrapped"] = True
        print(f"[bootstrap] {sym} mulai dari {ts[-1]}, posisi kosong")
        return info

    pos = pos_from_dict(sl.get("position"))
    pending = sl.get("pending")
    seen = pd.Timestamp(sl["last_bar"])
    start = int(ts.searchsorted(seen, side="right"))
    print(f"[run] {sym} sumber={source} bar={len(df)} "
          f"terakhir={sl['last_bar']} -> {len(df) - start} bar baru")

    # last_bar may only ever move FORWARD. The failover source can end a bar or
    # two behind the primary -- sanity_check() tolerates 3*BAR of staleness on
    # purpose -- and adopting its last bar would walk last_bar backwards, which
    # hands step() a bar it has already consumed. That is not a cosmetic replay:
    # a pending signal replayed this way is filled at the OPEN of its own signal
    # bar, so the ledger records an entry at a price that only existed before
    # the breakout happened. Hold the line instead and process nothing.
    if ts[-1] < seen:
        info["stale"] = True
        info["last_bar"] = sl["last_bar"]
        print(f"[run] {sym} PERINGATAN: feed berhenti di {ts[-1]} padahal bar "
              f"{sl['last_bar']} sudah diproses -- last_bar dipertahankan, "
              f"tidak ada bar yang diputar ulang")
        info["pos"] = pos
        return info

    for i in range(start, len(df)):
        pos, pending, events = step(f, ts, i, p, pos, pending)
        run["bars_processed"] += 1
        info["bars"] += 1
        for ev in events:
            run["events_emitted"] += 1
            queued += _handle(ev, sym, source, p, st)
        # Committed per bar, once that bar's events are in the ledger. If a later
        # bar raises -- step() cannot locate an entry bar that has scrolled out
        # of the 1000-bar window, say -- last_bar still sits on the last bar that
        # fully succeeded. Committing only at the end meant the whole batch was
        # replayed on the next run, and events.csv / trades.csv have no dedup of
        # their own, so every retry appended the same rows again.
        sl.update(last_bar=ts[i].isoformat(), position=pos_to_dict(pos),
                  pending=pending)

    sl.update(last_bar=ts[len(df) - 1].isoformat(), position=pos_to_dict(pos),
              pending=pending)
    info["pos"] = pos
    return info


def main():
    cfg = load()
    p = cfg["params"]
    symbols = cfg["symbols"]
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

    # Fingerprinted BEFORE migrating, so the migration itself counts as a change
    # and gets written. Taking it afterwards would leave the v1 file on disk with
    # the process running v2 in memory -- and the next run would migrate again.
    state_before = _fingerprint(st)
    if st and not state.is_v2(st):
        st = state.migrate(st, cfg["symbol"])
        print(f"[migrasi] state v1 -> v{state.SCHEMA}: "
              f"{cfg['symbol']} dipindah ke symbols[], "
              f"{len(st.get('sent_ids', []))} sent_ids diberi prefix simbol")

    # Anything left over from a previous run goes out before this run's own work.
    sent, failed, dropped = _flush(st)

    queued, seen, down = [], {}, []
    for sym in symbols:
        # One symbol must never be able to take the other three down with it.
        # _process() already returns None for an unreachable feed; this catches
        # the rest -- e.g. step() raising because a position has been held
        # longer than the 1000-bar window, so its entry bar is no longer in the
        # index. The traceback is printed and the symbol is reported as down,
        # so the failure is loud in the log, in runs.csv and in the heartbeat.
        try:
            info = _process(sym, cfg, p, st, run, queued)
        except Exception as e:  # noqa: BLE001
            print(f"[run] {sym} ERROR: {type(e).__name__}: {e}")
            print(traceback.format_exc())
            info = None
        if info is None:
            down.append(sym)
        else:
            seen[sym] = info

    st.setdefault("outbox", []).extend(queued)
    s2, f2, d2 = _flush(st)
    sent, failed, dropped = sent + s2, f2, dropped + d2

    # A symbol dropped from SYMBOLS keeps its slot in the state file but stops
    # being processed, so an open position there freezes: its trailing stop is
    # never advanced again and no EXIT is ever recorded. Nothing errors -- the
    # forward test simply grows a trade that never closes. Say so loudly, and
    # make sure the run is always written to runs.csv while it is true.
    # A feed that ended behind a bar we have already processed. Nothing was
    # replayed (see _process), but it means this symbol stopped advancing, and a
    # symbol that quietly stops advancing is exactly what runs.csv exists for.
    stale = [s for s, v in seen.items() if v.get("stale")]

    orphans = [s for s, v in (st.get("symbols") or {}).items()
               if s not in symbols and (v.get("position") or v.get("pending"))]
    if orphans:
        for s in orphans:
            print(f"[run] PERINGATAN: {s} punya posisi/pending tapi tidak ada di "
                  f"SYMBOLS -- tidak diproses, trailing stop-nya berhenti berjalan")

    st["engine_version"] = ENGINE_VERSION
    if _fingerprint(st) != state_before:
        st["updated_at"] = pd.Timestamp.now(tz="UTC").isoformat()
        ledger.write_json(STATE, st)
    else:
        print("[run] state tidak berubah, position.json tidak ditulis ulang")

    if failed:
        telegram = f"failed_{failed}" if not sent else f"partial_{sent}/{sent + failed}"
    elif sent:
        telegram = "sent"
    else:
        telegram = "nothing_to_send"

    # runs.csv keeps its v1 column list on purpose: rotating it would split the
    # liveness record in two. Four symbols are folded into the existing columns,
    # and the per-symbol detail lives in events.csv, which has a symbol column.
    opens = {s: v["pos"] for s, v in seen.items() if v.get("pos")}
    sources = sorted({v["source"] for v in seen.values()})
    unreal = sum((seen[s]["last_close"] - q.entry_price) * q.side / q.r_usdt
                 for s, q in opens.items())
    run.update(
        data_source=(sources[0] if len(sources) == 1 else "mixed:" + ",".join(sources)),
        bars_available=max((v["bars_available"] for v in seen.values()), default=0),
        last_bar_utc=max((v["last_bar"] for v in seen.values()), default=""),
        position_open=bool(opens),
        position_side="|".join(
            f"{_short(s)}:{'long' if q.side > 0 else 'short'}" for s, q in opens.items()),
        position_signal_id=",".join(q.signal_id for q in opens.values()),
        unrealised_R=round(unreal, 3) if opens else "",
        telegram_ok=telegram, sheet_ok=ledger.sheet_status())

    # Problems are collected, not overwritten. Each `if` used to replace both
    # the status AND the message, so a run that lost every feed and then failed
    # to deliver reported only "1 pesan masih di outbox" -- and runs.csv is the
    # only place either fact is written down. Highest priority takes the status
    # column; every message is kept.
    problems = []                       # (priority, status, message)
    if down and len(down) == len(symbols):
        # Every feed unreachable is the outage the alert was written for.
        problems.append((5, "data_error", "semua simbol gagal: " + ",".join(down)))
        notify.send(notify.alert_message("data feed gagal",
                                         "semua simbol gagal: " + ", ".join(down)))
    elif down:
        # One symbol down must not page the user every 10 minutes; the daily
        # heartbeat reports it, and runs.csv records it for later.
        problems.append((2, "partial_data", "simbol gagal: " + ",".join(down)))
    if orphans:
        problems.append((3, "orphan_symbol",
                         "posisi menggantung di simbol yang tidak lagi dipantau: "
                         + ",".join(orphans)))
    if failed:
        problems.append((4, "delivery_error", f"{failed} pesan masih di outbox"))
    if stale:
        problems.append((1, "stale_feed",
                         "feed berhenti di belakang bar yang sudah diproses, "
                         "last_bar dipertahankan: " + ",".join(stale)))
    if problems:
        problems.sort(reverse=True)
        run["status"] = problems[0][1]
        run["message"] = "; ".join(m for _, _, m in problems)
    if _should_log_run(run):
        ledger.log_run(run)
    else:
        print("[run] idle, baris runs.csv ditahan (MEX_QUIET_IDLE)")
    print(f"[run] selesai: {run['events_emitted']} event, "
          f"{len(opens)}/{len(seen)} simbol punya posisi, kirim={telegram}"
          + (f", dibuang={dropped}" if dropped else "")
          + (f", simbol gagal: {','.join(down)}" if down else "")
          + (f", feed tertinggal: {','.join(stale)}" if stale else ""))

    # Exit non-zero so GitHub reports the failure immediately. The message stays
    # in the outbox either way, so the next run retries it regardless.
    return 1 if (failed or len(down) == len(symbols)) else 0


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
        # The key carries the symbol because strategy._sid() does not: across
        # ETH/DOGE/XRP/SOL, 148 of 439 backtest signals shared an id with
        # another symbol. Without the symbol here, the second symbol's message
        # would be silently swallowed as a duplicate.
        key = state.dedup_key(kind, symbol, signal_id)
        if key in st.get("sent_ids", []):
            print(f"[dedup] {key} sudah pernah terkirim, tidak diulang")
            return []
        return [{"key": key, "kind": kind, "signal_id": signal_id,
                 "symbol": symbol, "text": text,
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
