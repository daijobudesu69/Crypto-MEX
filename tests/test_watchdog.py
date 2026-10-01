"""Offline tests for run_watchdog.py (audit 2026-10-01 #6).

The watcher went silent for ~15 hours on 30 Sep and nothing paged. These pin
the three situations apart -- healthy, dead (restart it), hung (link the log) --
and that the watchdog neither spams nor forgets to say it is over.
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

import run_watchdog as w  # noqa: E402

PASS, FAIL = [], []
NOW = pd.Timestamp("2026-10-01T12:00:00Z")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + str(detail)}")


RUN = {"status": "in_progress", "created_at": "2026-10-01T07:00:00Z",
       "html_url": "https://github.com/x/y/actions/runs/1"}


def test_decide():
    msgs, disp, wd = w.decide(NOW, NOW - pd.Timedelta("65min"), None, {})
    check("baris 65 menit lalu (idle normal) -> sehat, diam", not msgs and not disp and wd == {})

    msgs, disp, wd = w.decide(NOW, NOW - pd.Timedelta("3h"), [], {})
    check("mati, tidak ada run -> alarm + watcher dinyalakan",
          disp and len(msgs) == 1 and "Tidak ada watcher" in msgs[0]
          and "dinyalakan otomatis" in msgs[0] and wd["down_since"], (msgs, disp, wd))
    check("alarm menyebut stop tetap aktif tapi tidak digeser", "tidak digeser" in msgs[0])

    msgs, disp, _ = w.decide(NOW, NOW - pd.Timedelta("3h"), [RUN], {})
    check("run masih jalan tapi tidak menyimpan -> alarm 'macet' + link log, tanpa dispatch",
          not disp and "macet" in msgs[0] and RUN["html_url"] in msgs[0], msgs)

    msgs, disp, _ = w.decide(NOW, NOW - pd.Timedelta("3h"), None, {})
    check("API GitHub tak terbaca -> tetap alarm, tidak dispatch buta",
          not disp and "tidak terbaca" in msgs[0], msgs)

    wd = {"down_since": "2026-10-01T09:00:00+00:00", "last_alert": (NOW - pd.Timedelta("1h")).isoformat(),
          "dispatched_at": (NOW - pd.Timedelta("20min")).isoformat()}
    msgs, disp, wd2 = w.decide(NOW, NOW - pd.Timedelta("3h"), [], wd)
    check("alarm tidak diulang < 3 jam, dispatch tidak diulang < 60 menit",
          not msgs and not disp and wd2["down_since"] == wd["down_since"], (msgs, disp))
    wd["last_alert"] = (NOW - pd.Timedelta("3h")).isoformat()
    wd["dispatched_at"] = (NOW - pd.Timedelta("61min")).isoformat()
    msgs, disp, _ = w.decide(NOW, NOW - pd.Timedelta("4h"), [], wd)
    check("setelah 3 jam alarm diulang, setelah 60 menit dispatch diulang", msgs and disp)

    msgs, disp, wd3 = w.decide(NOW, NOW - pd.Timedelta("5min"), [RUN], wd)
    check("pulih -> satu pesan 'pulih', state watchdog dikosongkan",
          len(msgs) == 1 and "pulih" in msgs[0] and wd3 == {}, (msgs, wd3))

    msgs, disp, _ = w.decide(NOW, None, [], {})
    check("runs.csv kosong/tidak ada -> dianggap mati", disp and "belum pernah" in msgs[0], msgs)


def test_last_watcher_run():
    work = tempfile.mkdtemp()
    try:
        path = os.path.join(work, "runs.csv")
        with open(os.path.join(work, "runs.v1.csv"), "w", encoding="utf-8") as fh:
            fh.write("run_at_utc,status\n2026-09-01T00:00:00+00:00,ok\n")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("run_at_utc,status,x\n"
                     "2026-10-01T10:00:00+00:00,ok,1\n"
                     "2026-10-01T10:30:00+00:00,partial_data,1\n"
                     "2026-10-01T11:00:00+00:00,heartbeat,1\n"
                     "rusak,ok,1\n")
        got = w.last_watcher_run(path)
        check("baris watcher terbaru dipakai; heartbeat & baris rusak diabaikan",
              got == pd.Timestamp("2026-10-01T10:30:00Z"), got)
        check("tanpa file -> None", w.last_watcher_run(os.path.join(work, "nope.csv")) is None)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_driver():
    from mex import notify
    work, cwd = tempfile.mkdtemp(), os.getcwd()
    real = (notify.send, notify.configured, w.active_runs, w.dispatch_watcher)
    sent, calls, up = [], [], {"ok": True}
    notify.send = lambda t: (sent.append(t) or True) if up["ok"] else False
    notify.configured = lambda: True
    try:
        os.chdir(work)
        os.makedirs("state")
        fresh = pd.Timestamp.now(tz="UTC") - pd.Timedelta("10min")
        with open(w.RUNS, "w", encoding="utf-8") as fh:
            fh.write(f"run_at_utc,status\n{fresh.isoformat()},ok\n")
        w.active_runs = lambda: calls.append("api") or []
        w.dispatch_watcher = lambda: calls.append("dispatch") or True
        check("sehat -> exit 0, tidak ada pesan, tidak memanggil API, tidak menulis state",
              w.main() == 0 and not sent and not calls and not os.path.exists(w.STATE), (sent, calls))

        old = pd.Timestamp.now(tz="UTC") - pd.Timedelta("4h")
        with open(w.RUNS, "w", encoding="utf-8") as fh:
            fh.write(f"run_at_utc,status\n{old.isoformat()},ok\n")
        rc = w.main()
        st = json.load(open(w.STATE, encoding="utf-8"))
        check("mati -> exit 1 (merah), alarm terkirim, watcher dinyalakan, tercatat",
              rc == 1 and len(sent) == 1 and "dispatch" in calls
              and st.get("dispatched_at") and st.get("last_alert"), (rc, sent, calls, st))
        w.main()
        check("run berikutnya tidak spam", len(sent) == 1 and calls.count("dispatch") == 1, (sent, calls))

        with open(w.RUNS, "w", encoding="utf-8") as fh:
            fh.write(f"run_at_utc,status\n{fresh.isoformat()},ok\n")
        up["ok"] = False
        w.main()
        check("pesan 'pulih' gagal terkirim -> catatan gangguan disimpan untuk dicoba lagi",
              os.path.exists(w.STATE))
        up["ok"] = True
        check("pulih -> exit 0 + pesan pulih + state watchdog dihapus",
              w.main() == 0 and "pulih" in sent[-1] and not os.path.exists(w.STATE), sent)
    finally:
        notify.send, notify.configured, w.active_runs, w.dispatch_watcher = real
        os.chdir(cwd)
        shutil.rmtree(work, ignore_errors=True)


def test_workflow_file():
    with open(os.path.join(ROOT, ".github", "workflows", "watchdog.yml"), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    on = doc.get("on") or doc.get(True)
    check("watchdog terjadwal (cron) + bisa manual", "schedule" in on and "workflow_dispatch" in on)
    check("izin actions: write (baca run + dispatch) dan contents: write",
          doc["permissions"] == {"contents": "write", "actions": "write"}, doc["permissions"])
    check("grup concurrency sendiri (tidak antre di belakang watcher 5,5 jam)",
          doc["concurrency"]["group"] not in ("mex-state", "mex-heartbeat-state"))
    steps = doc["jobs"]["watchdog"]["steps"]
    check("state watchdog tetap disimpan walau langkah cek merah",
          any("save_state.sh" in s.get("run", "") and s.get("if") == "always()" for s in steps))
    check("tidak ada kunci API wallet di watchdog",
          "HL_AGENT_KEY" not in json.dumps(doc) and "HYPE_API" not in json.dumps(doc))


if __name__ == "__main__":
    print("test_watchdog.py")
    for t in (test_decide, test_last_watcher_run, test_driver, test_workflow_file):
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    sys.exit(1 if FAIL else 0)
