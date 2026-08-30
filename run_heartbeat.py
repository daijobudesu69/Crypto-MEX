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

from mex import datafeed, ledger, notify  # noqa: E402
from mex.config import load, ENGINE_VERSION  # noqa: E402

STATE = "state/position.json"
RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")
SHA = os.environ.get("GITHUB_SHA", "")[:8]


def _counts():
    """Signal / trade counts and cumulative R from the committed ledger."""
    out = {"signals_30d": 0, "trades_30d": 0, "trades_total": 0, "sum_R": 0.0}
    cut = pd.Timestamp.now(tz="UTC") - pd.Timedelta("30D")
    if os.path.exists(ledger.EVENTS) and os.path.getsize(ledger.EVENTS):
        e = pd.read_csv(ledger.EVENTS)
        if len(e):
            e["bar_time_utc"] = pd.to_datetime(e["bar_time_utc"], utc=True, errors="coerce")
            out["signals_30d"] = int(((e["event"] == "SIGNAL") & (e["bar_time_utc"] >= cut)).sum())
    if os.path.exists(ledger.TRADES) and os.path.getsize(ledger.TRADES):
        t = pd.read_csv(ledger.TRADES)
        if len(t):
            t["exit_bar_utc"] = pd.to_datetime(t["exit_bar_utc"], utc=True, errors="coerce")
            out["trades_total"] = int(len(t))
            out["trades_30d"] = int((t["exit_bar_utc"] >= cut).sum())
            out["sum_R"] = float(pd.to_numeric(t["result_R"], errors="coerce").sum())
    return out


def main():
    cfg = load()
    st = ledger.read_json(STATE, {})
    pos = st.get("position")

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
        "data_ok": data_ok, "error": err, **_counts(),
    }
    ok = notify.send(notify.heartbeat_message(s))

    ledger.log_run({
        "run_at_utc": s["now"], "status": "heartbeat" if data_ok else "heartbeat_data_error",
        "data_source": source, "last_bar_utc": st.get("last_bar", ""),
        "position_open": bool(pos), "position_side": (
            "long" if pos and pos["side"] > 0 else "short" if pos else ""),
        "position_signal_id": pos["signal_id"] if pos else "",
        "unrealised_R": round(unreal, 3) if pos else "",
        "telegram_ok": ok, "sheet_ok": ledger.sheet_configured(),
        "engine_version": ENGINE_VERSION, "run_id": RUN_ID, "commit_sha": SHA,
        "message": err,
    })
    print(f"[heartbeat] data_ok={data_ok} telegram={ok} posisi={'ya' if pos else 'tidak'}")
    # A broken feed must not fail the job, or the daily heartbeat would stop
    # being delivered exactly when it matters most.
    return 0


if __name__ == "__main__":
    sys.exit(main())
