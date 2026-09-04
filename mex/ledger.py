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

Every row is also mirrored to Google Sheets when that is configured -- via a
service account (preferred) or an Apps Script webhook. Failure to reach either
never blocks the run: the CSVs remain the source of truth.
"""
from . import compat  # noqa: F401
import csv
import json
import os

import requests

from . import sheets

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


def _rotate(path, cols):
    """Retire a log whose header no longer matches `cols`, keeping the old rows.

    DictWriter writes positionally, so appending today's 43-column row under a
    header written by an older column list produces a file that pandas refuses
    to parse at all ("Expected 3 fields, saw 45") -- and these logs are
    append-only, so that damage is permanent and takes the daily heartbeat down
    with it. Rotating to events.v2.csv keeps every historical row readable and
    starts the new schema in a clean file.
    """
    with open(path, encoding="utf-8", newline="") as fh:
        head = next(csv.reader(fh), None)
    if head == list(cols):
        return
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{stem}.v{n}{ext}"):
        n += 1
    os.replace(path, f"{stem}.v{n}{ext}")
    print(f"[ledger] header {os.path.basename(path)} berubah "
          f"({len(head or [])} -> {len(cols)} kolom); "
          f"log lama diarsipkan ke {os.path.basename(stem)}.v{n}{ext}")


def _append(path, cols, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and os.path.getsize(path):
        _rotate(path, cols)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in cols})


def log_event(row):
    _append(EVENTS, EVENT_COLS, row)
    _mirror("event", "events", EVENT_COLS, row)


def log_trade(row):
    _append(TRADES, TRADE_COLS, row)
    _mirror("trade", "trades", TRADE_COLS, row)


def log_run(row):
    _append(RUNS, RUN_COLS, row)
    _mirror("run", "runs", RUN_COLS, row)


def last_run_at() -> str | None:
    """Timestamp of the most recent row in runs.csv, or None if there is none.

    The polling watcher checks every 10 minutes. Logging a row each time would
    add 144 rows and 144 commits a day for a strategy that produces roughly four
    signals a month, so idle rows are rate-limited against this value.
    """
    if not (os.path.exists(RUNS) and os.path.getsize(RUNS)):
        return None
    try:
        with open(RUNS, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        return rows[-1]["run_at_utc"] if rows else None
    except Exception as e:  # noqa: BLE001
        print(f"[ledger] gagal membaca baris terakhir runs.csv: {type(e).__name__}")
        return None


def _mirror(kind, tab, cols, row):
    """Service account first, Apps Script webhook as the alternative."""
    if sheets.configured():
        ok = sheets.append(tab, cols, row)
        _PUSHES.append(ok)
        return ok
    return _push(kind, row)


# Outcome of every webhook POST made during this process, so a run can report
# whether the mirror actually received the rows rather than merely whether a URL
# was configured -- a silently failing webhook is exactly the failure mode that
# would otherwise go unnoticed for weeks.
_PUSHES: list[bool] = []


def _looks_like_webhook(url: str) -> bool:
    """Guard against a non-URL being pasted into the secret.

    A service-account JSON key pasted here would otherwise be handed to
    requests, whose exception text repeats the value -- leaking a private key
    into public workflow logs. Validate the shape before it is ever used.
    """
    return url.startswith("https://") and chr(10) not in url and len(url) < 2048


def _push(kind, row):
    """Best-effort mirror to a Google Sheets webhook. Never raises."""
    url = os.environ.get("GSHEET_WEBHOOK_URL", "").strip()
    if not url:
        return None
    if not _looks_like_webhook(url):
        print("[sheet] GSHEET_WEBHOOK_URL bukan URL https:// -- baris tidak dikirim.")
        _PUSHES.append(False)
        return False
    ok = False
    try:
        r = requests.post(url, json={"kind": kind, "row": row},
                          timeout=25, headers={"Content-Type": "application/json"})
        ok = r.status_code < 400
        if not ok:
            print(f"[sheet] HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        # Type only, never the message: requests embeds the full URL in its
        # exception text, and GitHub's secret masking does not cover a
        # multi-line secret. Printing it would leak the secret into public logs.
        print(f"[sheet] POST gagal: {type(e).__name__}")
    _PUSHES.append(ok)
    return ok


def sheet_configured() -> bool:
    return sheets.configured() or bool(os.environ.get("GSHEET_WEBHOOK_URL", "").strip())


def sheet_status() -> str:
    """What actually happened to the mirror this run: ok / partial / failed."""
    if not sheet_configured():
        return "not_configured"
    if not _PUSHES:
        reach = sheets.reachable() if sheets.configured() else sheet_reachable()
        return "ok" if reach else "unreachable"
    if all(_PUSHES):
        return "ok"
    return "failed" if not any(_PUSHES) else f"partial_{sum(_PUSHES)}/{len(_PUSHES)}"


def sheet_reachable() -> bool:
    """GET the Apps Script (doGet answers {ok:true}) without appending a row."""
    url = os.environ.get("GSHEET_WEBHOOK_URL", "").strip()
    if not url:
        return False
    if not _looks_like_webhook(url):
        print("[sheet] GSHEET_WEBHOOK_URL bukan URL https:// -- diabaikan. "
              "Isinya harus URL web app Apps Script, BUKAN file kunci JSON.")
        return False
    try:
        r = requests.get(url, timeout=25)
        return r.status_code < 400 and '"ok"' in r.text
    except Exception as e:  # noqa: BLE001
        print(f"[sheet] probe gagal: {type(e).__name__}")
        return False


class StateCorrupt(RuntimeError):
    """position.json exists but could not be parsed."""


def read_json(path, default):
    """Missing file -> default. Unreadable file -> raise, never default.

    Falling back to `default` on a corrupt file would look like a first-ever run:
    the driver would bootstrap, adopt the newest bar and declare itself flat --
    silently abandoning an open position and its trailing stop. A hard failure
    that pages the user is the only safe answer.
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise StateCorrupt(f"{path} rusak dan tidak bisa dibaca: {e}") from e


def write_json(path, obj):
    """Atomic: write a sibling temp file, then rename over the target.

    open(path, "w") truncates before it writes, so a job killed in between --
    GitHub cancels at timeout-minutes -- leaves a half-written state file.
    os.replace() is atomic on POSIX and Windows, so the file on disk is always
    either the old state or the new one, never a fragment of either.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
