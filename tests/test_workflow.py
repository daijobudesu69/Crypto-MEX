"""The watcher loop in signal.yml must survive any script exiting non-zero.

GitHub runs a step's `run:` as `bash -e`. On 2026-09-30 run_executor.py exited 1
(a bad secret), `bash -e` ended the whole job right there -- before
save_state.sh -- and every job after it died the same way: no state saved, no
signal sent, no watcher alive from 12:08 UTC for ~15 hours. A comment above the
line even said a failing executor must not stop the loop.

Two checks:
  1. behavioural: run the real loop script under `bash -e` with run_signal and
     run_executor stubbed to FAIL, and require save_state to still run and the
     job to end at the loop's own exit;
  2. static: every command that can fail inside any workflow's run script is
     guarded (`|| ...`, or inside `if`).
"""
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mex.compat  # noqa: F401,E402

import yaml  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + str(detail)}")


def run_scripts():
    for fn in sorted(os.listdir(WF)):
        if not fn.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(WF, fn), encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        for job in (doc.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if "run" in step:
                    yield fn, step.get("name", "?"), step["run"]


def test_loop_survives_failures():
    script = next(run for fn, name, run in run_scripts()
                  if fn == "signal.yml" and "run_executor.py" in run)
    bash = shutil.which("bash")
    if not bash:
        check("bash tersedia untuk uji loop", bool(os.environ.get("CI")) is False, "bash tidak ada di CI")
        print("  SKIP  bash tidak ditemukan di mesin ini")
        return
    stubs = r'''
python() {
  case "$1" in
    run_signal.py)    echo "[stub] run_signal gagal";   return 1 ;;
    run_executor.py)  echo "[stub] run_executor gagal (kunci: ${HL_AGENT_KEY:-kosong})"; return 2 ;;
    run_heartbeat.py) echo "[stub] heartbeat"; return 1 ;;
  esac
  return 0
}
bash() { echo "[stub] bash $*"; return 0; }
'''
    env = dict(os.environ, MODE="once", HL_AGENT_KEY="kunci-uji")
    p = subprocess.run([bash, "-e", "-c", stubs + script], capture_output=True, text=True,
                       env=env, cwd=ROOT, timeout=60)
    out = p.stdout + p.stderr
    check("run_signal gagal -> loop lanjut ke executor", "[stub] run_executor gagal" in out, out[-600:])
    check("executor gagal -> state TETAP disimpan", "tools/save_state.sh" in out, out[-600:])
    check("loop selesai di exit-nya sendiri, bukan dibunuh bash -e",
          "[watch] selesai setelah 1 kali cek (exit 2)" in out, out[-600:])
    check("job tetap merah supaya kegagalan terlihat", p.returncode == 2, p.returncode)
    check("kunci hanya diteruskan ke executor", "kunci: kunci-uji" in out)


# A line is safe when bash -e cannot abort on it.
SAFE = re.compile(r"(\|\||^if |^elif |^while |^until |^then|^do\b|^done|^fi\b|^else|^for |"
                  r"^echo |^set |^unset |^export |^[A-Za-z_][A-Za-z0-9_]*=[^ ]*$|^#|^$|"
                  r"^\w+\(\)|^case |^esac|^\)|^;;|^break|^continue|^sleep |^exit |^n=|^worst=|"
                  r"^rc=|^local |^\{|^\}|^[\]\)]|^git config |^pip install )")


def test_every_command_guarded():
    # A failing command is only dangerous when something after it in the same
    # script still has to run (save_state, the rest of the loop). A step that is
    # one test command SHOULD fail with it.
    risky = []
    for fn, name, run in run_scripts():
        cmds = [ln.strip() for ln in run.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
        for i, line in enumerate(cmds):
            if i == len(cmds) - 1 or SAFE.search(line):
                continue
            # Commands that can exit non-zero: interpreters and repo scripts.
            if re.search(r"(^|\s)(python|bash)\s", line) or re.search(r"\.(py|sh)\b", line):
                risky.append(f"{fn} [{name}]: {line}")
    check("semua perintah python/bash yang masih diikuti perintah lain dilindungi dari bash -e",
          not risky, risky)


if __name__ == "__main__":
    print("test_workflow.py")
    for t in (test_loop_survives_failures, test_every_command_guarded):
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    sys.exit(1 if FAIL else 0)
