"""Daily liveness check. Exactly one message per day, signal or no signal.

Its job is to answer "is this thing still running?" without ever being confused
with a trade alert. It also actively probes the data feed, so a silent pipeline
that has quietly lost its data source shows up within 24 hours rather than being
discovered the day a signal fails to arrive.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402

from mex import datafeed, ledger, notify, sheets  # noqa: E402
from mex.config import load, ENGINE_VERSION  # noqa: E402

STATE = "state/position.json"
RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")
SHA = os.environ.get("GITHUB_SHA", "")[:8]


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
        if os.path.exists(ledger.EVENTS) and os.path.getsize(ledger.EVENTS):
            e = pd.read_csv(ledger.EVENTS)
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
        if os.path.exists(ledger.TRADES) and os.path.getsize(ledger.TRADES):
            t = pd.read_csv(ledger.TRADES)
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
        if not (os.path.exists(ledger.RUNS) and os.path.getsize(ledger.RUNS)):
            return ""
        r = pd.read_csv(ledger.RUNS)
        r["run_at_utc"] = pd.to_datetime(r["run_at_utc"], utc=True, errors="coerce")
        r = r[r["run_at_utc"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta("24h")]
        s = r["sheet_ok"].astype(str)
        if not len(s):
            return ""
        bad = int((~s.isin(["ok", "not_configured"])).sum())
        if s.eq("not_configured").all():
            return "tidak dikonfigurasi"
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
    pos = st.get("position")

    hint = sheets.missing()
    if hint:
        print(f"[sheets] {hint}")

    data_ok, source, err, last_close = True, "-", "", None
    try:
        # 500, not 300: sanity_check needs >=300 CLOSED bars and the newest
        # bar is always dropped as still forming.
        feed = datafeed.fetch(limit=500, prefer=cfg["prefer_source"])
        source = feed.source
        last_close = float(feed.df["close"].iloc[-1])
    except Exception as e:  # noqa: BLE001
        data_ok, err = False, f"{type(e).__name__}: {e}"

    unreal = 0.0
    if pos and last_close is not None and pos.get("r_usdt"):
        unreal = (last_close - pos["entry_price"]) * pos["side"] / pos["r_usdt"]

    s = {
        "now": pd.Timestamp.now(tz="UTC").isoformat(),
        "last_bar": st.get("last_bar"), "source": source,
        "position": pos, "unrealised_R": round(unreal, 2),
        "data_ok": data_ok, "error": err,
        "outbox_pending": len(st.get("outbox", [])), **_counts(),
    }
    ok = notify.send(notify.heartbeat_message(s))

    ledger.log_run({
        "run_at_utc": s["now"], "status": "heartbeat" if data_ok else "heartbeat_data_error",
        "data_source": source, "last_bar_utc": st.get("last_bar", ""),
        "position_open": bool(pos), "position_side": (
            "long" if pos and pos["side"] > 0 else "short" if pos else ""),
        "position_signal_id": pos["signal_id"] if pos else "",
        "unrealised_R": round(unreal, 3) if pos else "",
        "telegram_ok": ok, "sheet_ok": ledger.sheet_status(),
        "engine_version": ENGINE_VERSION, "run_id": RUN_ID, "commit_sha": SHA,
        "message": err,
    })
    print(f"[heartbeat] data_ok={data_ok} telegram={ok} posisi={'ya' if pos else 'tidak'}")
    # A broken feed must not fail the job, or the daily heartbeat would stop
    # being delivered exactly when it matters most.
    return 0


if __name__ == "__main__":
    sys.exit(main())
