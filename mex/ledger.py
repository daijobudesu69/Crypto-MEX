"""Append-only logs: the forward test's evidence base.

Three files, all committed back to the repo so the history is auditable and no
external service is required for the record to survive:

  state/events.csv  -- one row per SIGNAL / ENTRY / EXIT, with the full indicator
                       snapshot at that bar. This is what lets us ask later why a
                       signal fired, not just that it did.
  state/trades.csv  -- one row per completed round trip, the evaluation table.
  state/runs.csv    -- one row per workflow run: liveness, latency, data source.

Columns are deliberately wide. Adding a column later cannot recover data that was
never written, and this is a live forward test -- there is no re-run.

If GSHEET_WEBHOOK_URL is set, every row is also POSTed to that endpoint (a Google
Apps Script web app is the simplest receiver). Failure to reach it never blocks
the run: the CSVs remain the source of truth.
"""
from . import compat  # noqa: F401
import csv
import json
import os

import requests

EVENTS = "state/events.csv"
TRADES = "state/trades.csv"
RUNS = "state/runs.csv"

EVENT_COLS = [
    # identity / provenance
    "logged_at_utc", "event", "signal_id", "bar_time_utc", "side", "symbol",
    "data_source", "engine_version", "run_id", "commit_sha",
    "signal_to_send_minutes",
    # the actionable numbers
    "ref_price", "entry_zone_low", "entry_zone_high", "expires_at_utc",
    "r_usdt", "callback_pct", "stop_level", "r_pct_of_price",
    # realised (paper) fill
    "entry_price", "exit_price", "bars_held", "trail_at_event", "exit_reason",
    # indicator snapshot at this bar
    "open", "high", "low", "close", "volume", "atr14", "atr_pct_of_price",
    "rsi", "rsi_roc", "ema_fast", "ema_slow", "ema_spread_pct",
    "vol_avg", "vol_ratio", "prior_high", "prior_rsi_peak", "breakout_margin_pct",
    # left blank for the human to fill in
    "actual_fill_price", "actual_qty", "actual_exit_price", "notes",
]

TRADE_COLS = [
    "signal_id", "symbol", "side", "data_source", "engine_version",
    "signal_bar_utc", "entry_bar_utc", "exit_bar_utc",
    "ref_price", "entry_price", "exit_price", "stop_initial", "final_trail",
    "r_usdt", "callback_pct",
    "bars_held", "hours_held", "ret_pct", "result_R",
    "mae_pct", "mfe_pct", "mfe_R", "giveback_pct", "exit_reason",
    "entry_atr14", "entry_rsi", "entry_rsi_roc", "entry_vol_ratio",
    "entry_ema_spread_pct", "entry_breakout_margin_pct",
    "signal_to_send_minutes",
    "actual_fill_price", "actual_qty", "actual_exit_price", "actual_pnl", "notes",
]

RUN_COLS = [
    "run_at_utc", "status", "data_source", "bars_available", "last_bar_utc",
    "bars_processed", "events_emitted", "position_open", "position_side",
    "position_signal_id", "unrealised_R", "telegram_ok", "sheet_ok",
    "engine_version", "run_id", "commit_sha", "message",
]


def _append(path, cols, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in cols})


def log_event(row):
    _append(EVENTS, EVENT_COLS, row)
    _push("event", row)


def log_trade(row):
    _append(TRADES, TRADE_COLS, row)
    _push("trade", row)


def log_run(row):
    _append(RUNS, RUN_COLS, row)
    _push("run", row)


# Outcome of every webhook POST made during this process, so a run can report
# whether the mirror actually received the rows rather than merely whether a URL
# was configured -- a silently failing webhook is exactly the failure mode that
# would otherwise go unnoticed for weeks.
_PUSHES: list[bool] = []


def _push(kind, row):
    """Best-effort mirror to a Google Sheets webhook. Never raises."""
    url = os.environ.get("GSHEET_WEBHOOK_URL", "").strip()
    if not url:
        return None
    ok = False
    try:
        r = requests.post(url, json={"kind": kind, "row": row},
                          timeout=25, headers={"Content-Type": "application/json"})
        ok = r.status_code < 400
        if not ok:
            print(f"[sheet] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"[sheet] {type(e).__name__}: {e}")
    _PUSHES.append(ok)
    return ok


def sheet_configured() -> bool:
    return bool(os.environ.get("GSHEET_WEBHOOK_URL", "").strip())


def sheet_status() -> str:
    """What actually happened to the mirror this run: ok / partial / failed."""
    if not sheet_configured():
        return "not_configured"
    if not _PUSHES:
        return "ok" if sheet_reachable() else "unreachable"
    if all(_PUSHES):
        return "ok"
    return "failed" if not any(_PUSHES) else f"partial_{sum(_PUSHES)}/{len(_PUSHES)}"


def sheet_reachable() -> bool:
    """GET the Apps Script (doGet answers {ok:true}) without appending a row."""
    url = os.environ.get("GSHEET_WEBHOOK_URL", "").strip()
    if not url:
        return False
    try:
        r = requests.get(url, timeout=25)
        return r.status_code < 400 and '"ok"' in r.text
    except Exception as e:  # noqa: BLE001
        print(f"[sheet] probe gagal: {type(e).__name__}: {e}")
        return False


def read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=str)
