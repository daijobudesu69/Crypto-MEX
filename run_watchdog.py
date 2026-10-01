"""Watchdog: page when the watcher stops saving state, and restart it if it is gone.

.github/workflows/watchdog.yml, a separate cron (audit 2026-10-01 #6).

Before this, a dead watcher was noticed through the daily heartbeat or a GitHub
failure e-mail. On 30 Sep it stayed dead for ~15 hours. With real positions,
a dead watcher means the stops on the exchange still work but are no longer
trailed, and strategy exits are not executed.

The signal: the newest watcher row in state/runs.csv. The watcher writes one
at least every 60 minutes even when idle (MEX_QUIET_IDLE), and every row is
committed in the same cycle, so on origin/main it is never older than ~70
minutes while things work. Older than STALE means no watcher is saving.

Then GitHub's API says why: a signal.yml run still in progress (hung, or
failing to save -> its log is linked), or none at all (dead -> a new one is
dispatched once an hour, which is also exactly what the owner would do by hand).

Alerts repeat every REALERT while it lasts; recovery is announced once.
Watchdog state lives in state/watchdog.json (written only on a change).
GitHub cron fires unreliably (~25% of slots measured on this repo), so with
four slots an hour it runs roughly hourly -- still an hour, not fifteen.
"""
import csv
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402
import requests  # noqa: E402

from mex import notify  # noqa: E402

RUNS = "state/runs.csv"
STATE = "state/watchdog.json"
STALE = pd.Timedelta("90min")
REALERT = pd.Timedelta("3h")
REDISPATCH = pd.Timedelta("60min")
API = "https://api.github.com"
WORKFLOW = "signal.yml"
ACTIVE = ("in_progress", "queued", "waiting", "requested", "pending")


def last_watcher_run(path=RUNS):
    """Time of the newest runs.csv row written by run_signal.py (not the heartbeat)."""
    stem, ext = os.path.splitext(path)
    newest = None
    for pth in sorted(glob.glob(f"{stem}.v*{ext}")) + [path]:
        if not (os.path.exists(pth) and os.path.getsize(pth)):
            continue
        with open(pth, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("status") == "heartbeat":
                    continue
                try:
                    t = pd.Timestamp(row.get("run_at_utc"))
                except Exception:  # noqa: BLE001
                    continue
                if pd.isna(t):
                    continue
                t = t.tz_localize("UTC") if t.tzinfo is None else t
                if newest is None or t > newest:
                    newest = t
    return newest


def _fmt_age(td) -> str:
    m = int(td.total_seconds() // 60)
    return f"{m // 60} jam {m % 60} menit" if m >= 60 else f"{m} menit"


def decide(now, last, active, wd):
    """Pure decision. Returns (messages, dispatch, new_wd).

    active: list of {"status", "created_at", "html_url"} for signal.yml runs that
    are not finished, or None when GitHub's API could not be read.
    """
    wd = dict(wd or {})
    age = None if last is None else now - last
    if age is not None and age <= STALE:
        msgs = []
        if wd.get("down_since"):
            msgs.append(f"✅ <b>Watcher MEX pulih</b>: state tersimpan lagi {last:%Y-%m-%d %H:%M} UTC "
                        f"(terhenti sejak {wd['down_since'][:16].replace('T', ' ')} UTC).")
        return msgs, False, {}

    wd.setdefault("down_since", (last or now).isoformat())
    dispatch = (active == [] and (not wd.get("dispatched_at")
                                  or now - pd.Timestamp(wd["dispatched_at"]) >= REDISPATCH))
    msgs = []
    if not wd.get("last_alert") or now - pd.Timestamp(wd["last_alert"]) >= REALERT:
        since = "belum pernah" if last is None else (
            f"sejak {last:%Y-%m-%d %H:%M} UTC ({_fmt_age(age)} lalu)")
        text = f"🚨 <b>Watcher MEX tidak menyimpan state</b> {since}.\n"
        if active is None:
            text += "Status run GitHub tidak terbaca. "
        elif active:
            r = active[0]
            text += (f"Ada run {notify.esc(r.get('status'))} sejak "
                     f"{notify.esc(str(r.get('created_at'))[:16].replace('T', ' '))} UTC, tapi tidak "
                     f"menyimpan state -- kemungkinan macet atau gagal simpan. Log: "
                     f"{notify.esc(r.get('html_url'))} ")
        else:
            text += ("Tidak ada watcher yang hidup. "
                     + ("Watcher baru dinyalakan otomatis. " if dispatch else ""))
        text += ("\nStop di bursa tetap aktif, tapi tidak digeser dan exit strategi tidak "
                 "dieksekusi sampai watcher jalan lagi.")
        msgs.append(text)
    return msgs, dispatch, wd


def _gh(method, path, **kw):
    token = os.environ.get("GITHUB_TOKEN", "")
    return requests.request(method, f"{API}/repos/{os.environ['GITHUB_REPOSITORY']}{path}",
                            headers={"Authorization": f"Bearer {token}",
                                     "Accept": "application/vnd.github+json"},
                            timeout=20, **kw)


def active_runs():
    """Unfinished signal.yml runs, or None if the API could not be read."""
    try:
        # Newest first; anything not completed (in_progress, queued, and the
        # "pending" a concurrency group holds back) counts as a live watcher.
        r = _gh("GET", f"/actions/workflows/{WORKFLOW}/runs", params={"per_page": 20})
        r.raise_for_status()
        return [{"status": x.get("status"), "created_at": x.get("created_at"),
                 "html_url": x.get("html_url")} for x in r.json().get("workflow_runs", [])
                if x.get("status") in ACTIVE]
    except Exception as e:  # noqa: BLE001
        print(f"[watchdog] status run tidak terbaca: {type(e).__name__}: {e}")
        return None


def dispatch_watcher() -> bool:
    try:
        r = _gh("POST", f"/actions/workflows/{WORKFLOW}/dispatches",
                json={"ref": os.environ.get("GITHUB_REF_NAME", "main"), "inputs": {"mode": "loop"}})
        print(f"[watchdog] dispatch {WORKFLOW}: HTTP {r.status_code}")
        return r.status_code == 204
    except Exception as e:  # noqa: BLE001
        print(f"[watchdog] dispatch gagal: {type(e).__name__}: {e}")
        return False


def main() -> int:
    now = pd.Timestamp.now(tz="UTC")
    try:
        with open(STATE, encoding="utf-8") as fh:
            wd = json.load(fh)
    except (OSError, ValueError):
        wd = {}
    last = last_watcher_run()
    age = "?" if last is None else _fmt_age(now - last)
    print(f"[watchdog] baris watcher terakhir: {last} ({age} lalu)")
    active = None if last is not None and now - last <= STALE else active_runs()
    msgs, dispatch, new = decide(now, last, active, wd)
    if dispatch and dispatch_watcher():
        new["dispatched_at"] = now.isoformat()
    for m in msgs:
        if notify.send(m) or not notify.configured():
            if "🚨" in m:
                new["last_alert"] = now.isoformat()
        else:
            print("[watchdog] Telegram gagal; dicoba lagi di run berikutnya")
            if not new:
                new = wd          # keep the outage record until "pulih" is delivered
    if new != wd:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        if new:
            with open(STATE, "w", encoding="utf-8") as fh:
                json.dump(new, fh, indent=2)
        elif os.path.exists(STATE):
            os.remove(STATE)
    down = bool(new.get("down_since"))
    print(f"[watchdog] {'WATCHER MATI/MACET' if down else 'watcher sehat'}")
    # Red while down, so GitHub's own e-mail is a second channel.
    return 1 if down else 0


if __name__ == "__main__":
    sys.exit(main())
