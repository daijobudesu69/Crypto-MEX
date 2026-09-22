"""Daily liveness check. Exactly one message per day, signal or no signal.

Its job is to answer "is this thing still running?" without ever being confused
with a trade alert. It also actively probes the data feed, so a silent pipeline
that has quietly lost its data source shows up within 24 hours rather than being
discovered the day a signal fails to arrive.
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402

from mex import datafeed, ledger, notify, sheets, state  # noqa: E402
from mex.config import load, ENGINE_VERSION  # noqa: E402

STATE = "state/position.json"
RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")
SHA = os.environ.get("GITHUB_SHA", "")[:8]

# When the daily message is due, as HH:MM UTC. 00:00 UTC = 07:00 WIB, which is
# what the README has always promised.
TARGET_UTC = os.environ.get("MEX_HEARTBEAT_UTC", "00:00")


def _due(st) -> tuple[bool, str]:
    """Is today's heartbeat still owed? Returns (due, reason).

    This used to be decided purely by cron, and cron got it badly wrong: the
    00:07 UTC slot is the most congested in GitHub's queue and the message
    landed ~4 hours late every single day (measured 4h02m-4h10m over four
    consecutive days, worst 7h04m). The schedule is now driven by the signal
    watcher, which is alive continuously and checks every 10 minutes, so the
    decision has to live here where both callers can share it.

    MEX_FORCE_HEARTBEAT=1 overrides, for manual "send me one now" runs.
    """
    if os.environ.get("MEX_FORCE_HEARTBEAT") == "1":
        return True, "dipaksa (MEX_FORCE_HEARTBEAT=1)"
    now = pd.Timestamp.now(tz="UTC")
    today = now.strftime("%Y-%m-%d")
    if st.get("last_heartbeat_date") == today:
        return False, f"sudah dikirim hari ini ({today})"
    try:
        hh, mm = (int(x) for x in TARGET_UTC.split(":"))
        target = now.normalize() + pd.Timedelta(hours=hh, minutes=mm)
    except Exception:  # noqa: BLE001
        target = now.normalize()
    if now < target:
        return False, f"belum waktunya (target {TARGET_UTC} UTC)"
    return True, f"jatuh tempo untuk {today}"


def _ledger_frames(path):
    """The live log plus every archive ledger._rotate() left beside it.

    _rotate() moves the old file to <name>.v1.csv when the column list changes,
    which keeps the history readable -- but anything that opens only the live
    file sees an empty ledger. The day a column is added, the heartbeat would
    have announced "total sejak mulai: 0 transaksi - +0.00 R" with no error
    anywhere, which reads exactly like a forward test that has lost its record.
    """
    stem, ext = os.path.splitext(path)
    frames = []
    for pth in sorted(glob.glob(f"{stem}.v*{ext}")) + [path]:
        if not (os.path.exists(pth) and os.path.getsize(pth)):
            continue
        try:
            frames.append(pd.read_csv(pth))
        except Exception as e:  # noqa: BLE001
            print(f"[heartbeat] {os.path.basename(pth)} dilewati: {type(e).__name__}")
    return pd.concat(frames, ignore_index=True) if frames else None


def _counts():
    """Signal / trade counts and cumulative R from the committed ledger.

    Wrapped in its own try/except because a malformed CSV must not be able to
    take the heartbeat down: the liveness message is most valuable on exactly
    the day something else has gone wrong.
    """
    out = {"signals_30d": 0, "signals_30d_expired": 0, "trades_30d": 0,
           "trades_total": 0, "sum_R": 0.0, "mirror_24h": ""}
    cut = pd.Timestamp.now(tz="UTC") - pd.Timedelta("30D")
    try:
        e = _ledger_frames(ledger.EVENTS)
        if e is not None:
            if len(e):
                e["bar_time_utc"] = pd.to_datetime(e["bar_time_utc"], utc=True,
                                                   errors="coerce")
                recent = (e["event"] == "SIGNAL") & (e["bar_time_utc"] >= cut)
                # A signal that expired before it could be sent was never
                # actionable. Counting it as delivered hides the very cron
                # unreliability this number is supposed to expose.
                gone = e.get("exit_reason", pd.Series("", index=e.index)).fillna("")
                expired = recent & (gone == "EXPIRED_BEFORE_SEND")
                out["signals_30d"] = int((recent & ~expired).sum())
                out["signals_30d_expired"] = int(expired.sum())
        t = _ledger_frames(ledger.TRADES)
        if t is not None:
            if len(t):
                t["exit_bar_utc"] = pd.to_datetime(t["exit_bar_utc"], utc=True,
                                                   errors="coerce")
                out["trades_total"] = int(len(t))
                out["trades_30d"] = int((t["exit_bar_utc"] >= cut).sum())
                out["sum_R"] = float(pd.to_numeric(t["result_R"], errors="coerce").sum())
    except Exception as e:  # noqa: BLE001
        print(f"[heartbeat] gagal membaca ledger: {type(e).__name__}: {e}")
    out["mirror_24h"] = _mirror_24h()
    return out


def _mirror_24h() -> str:
    """How the Google Sheets mirror actually behaved over the last day.

    sheet_status() has always recorded failures into runs.csv, but nothing ever
    read that column back, so a mirror that had been refusing rows for a week
    looked identical to one that was working. Surfacing it once a day is what
    makes it findable.
    """
    try:
        r = _ledger_frames(ledger.RUNS)
        if r is None or "sheet_ok" not in r:
            return ""
        r["run_at_utc"] = pd.to_datetime(r["run_at_utc"], utc=True, errors="coerce")
        r = r[r["run_at_utc"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta("24h")]
        # dropna() FIRST: under pandas 3's str dtype, astype(str) leaves NaN as
        # NaN rather than turning it into the string "nan", so a missing value
        # matches neither the blank list below nor the ok list -- and silently
        # counted as a failure.
        s = r["sheet_ok"].dropna().astype(str).str.strip()
        # Blank/NaN is a row written before the mirror was reached at all (the
        # state_error path logs one), and "True"/"False" are the bare booleans an
        # older heartbeat wrote into this column -- runs.csv still holds three of
        # them. None of that is evidence of a failing mirror, and counting it as
        # one made the daily message cry wolf about a mirror that was working.
        s = s[~s.isin(["", "nan", "None", "<NA>"])]
        if not len(s):
            return ""
        if s.eq("not_configured").all():
            return "tidak dikonfigurasi"
        bad = int((~s.isin(["ok", "not_configured", "True", "true"])).sum())
        return "semua ok" if not bad else f"⚠️ {bad} dari {len(s)} run GAGAL"
    except Exception as e:  # noqa: BLE001
        print(f"[heartbeat] gagal membaca runs.csv: {type(e).__name__}: {e}")
        return ""


def main():
    cfg = load()
    try:
        st = ledger.read_json(STATE, {})
    except ledger.StateCorrupt as e:
        # Still send something: a corrupt state file is precisely the condition
        # the daily liveness message exists to surface.
        notify.send(notify.alert_message("state rusak", e))
        print(f"[heartbeat] {e}")
        return 1
    # Migrated in memory so every symbol can be reported. This is not read-only:
    # if the heartbeat goes out it marks the day and writes `st` back, which
    # persists the migration. That is safe -- migrate() is idempotent and the
    # signal driver produces the same shape -- and it matters on a day the
    # watcher is dead and heartbeat.yml is the only thing running.
    st = state.migrate(st, cfg["symbol"]) if st else st
    slots = st.get("symbols") or {}

    # Checked before anything else touches the network: the watcher calls this
    # every 10 minutes, and 143 of those 144 daily calls have nothing to do.
    due, why = _due(st)
    if not due:
        print(f"[heartbeat] dilewati -- {why}")
        return 0
    print(f"[heartbeat] {why}")

    hint = sheets.missing()
    if hint:
        print(f"[sheets] {hint}")

    # Probing every symbol, not just the primary: a feed that has quietly died
    # for one instrument is exactly what this daily message exists to surface,
    # and checking only ETH would have hidden it.
    positions, down, sources, errs = {}, [], [], []
    for sym in cfg["symbols"]:
        pos = (slots.get(sym) or {}).get("position")
        last_close = None
        try:
            # 500, not 300: sanity_check needs >=300 CLOSED bars and the newest
            # bar is always dropped as still forming.
            feed = datafeed.fetch(limit=500, prefer=cfg["prefer_source"], symbol=sym)
            sources.append(feed.source)
            last_close = float(feed.df["close"].iloc[-1])
        except Exception as e:  # noqa: BLE001
            down.append(sym)
            errs.append(f"{sym}: {type(e).__name__}")
        unreal = 0.0
        if pos and last_close is not None and pos.get("r_usdt"):
            unreal = (last_close - pos["entry_price"]) * pos["side"] / pos["r_usdt"]
        positions[sym] = {"position": pos, "unrealised_R": round(unreal, 2)}

    data_ok = not down
    err = "; ".join(errs)
    uniq = sorted(set(sources))
    source = uniq[0] if len(uniq) == 1 else (",".join(uniq) if uniq else "-")
    bars = [v.get("last_bar") for v in slots.values() if v.get("last_bar")]

    s = {
        "now": pd.Timestamp.now(tz="UTC").isoformat(),
        "last_bar": max(bars) if bars else None, "source": source,
        "positions": positions, "symbols_down": down,
        "data_ok": data_ok, "error": err,
        "outbox_pending": len(st.get("outbox", [])), **_counts(),
    }
    ok = notify.send(notify.heartbeat_message(s))
    pos = next((v["position"] for v in positions.values() if v["position"]), None)
    unreal = sum(v["unrealised_R"] for v in positions.values())

    # Only claim the day once it actually went out. A failed send leaves the day
    # unclaimed so the watcher's next 10-minute tick tries again -- previously a
    # failed heartbeat was simply lost until the next day's cron.
    if ok or not notify.configured():
        st["last_heartbeat_date"] = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
        ledger.write_json(STATE, st)
    else:
        print("[heartbeat] gagal terkirim; hari ini belum ditandai, akan dicoba lagi")

    opens = {sy: v["position"] for sy, v in positions.items() if v["position"]}
    ledger.log_run({
        "run_at_utc": s["now"], "status": "heartbeat" if data_ok else "heartbeat_data_error",
        "data_source": source, "last_bar_utc": s["last_bar"] or "",
        "position_open": bool(opens),
        "position_side": "|".join(
            f"{sy.replace('USDT', '')}:{'long' if q['side'] > 0 else 'short'}"
            for sy, q in opens.items()),
        "position_signal_id": ",".join(q["signal_id"] for q in opens.values()),
        "unrealised_R": round(unreal, 3) if opens else "",
        # A string, like run_signal writes. This column used to hold bare
        # booleans from here and words from there -- one column, two types.
        "telegram_ok": ("sent" if ok else
                        "not_configured" if not notify.configured() else "failed"),
        "sheet_ok": ledger.sheet_status(),
        "engine_version": ENGINE_VERSION, "run_id": RUN_ID, "commit_sha": SHA,
        "message": err,
    })
    print(f"[heartbeat] data_ok={data_ok} telegram={ok} posisi={'ya' if pos else 'tidak'}")
    # A broken feed must not fail the job, or the daily heartbeat would stop
    # being delivered exactly when it matters most.
    return 0


if __name__ == "__main__":
    sys.exit(main())
