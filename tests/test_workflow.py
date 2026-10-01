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
timeout() {
  # `timeout -k 30 900 cmd ...`: record the limit, then run cmd (a stub above).
  while [ "${1#-}" != "$1" ]; do shift 2; done
  echo "[stub] timeout $1"; shift
  "$@"
}
'''
    env = dict(os.environ, MODE="once", HL_AGENT_KEY="kunci-uji")
    p = subprocess.run([bash, "-e", "-c", stubs + script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, cwd=ROOT, timeout=60)
    out = (p.stdout or "") + (p.stderr or "")
    check("run_signal gagal -> loop lanjut ke executor", "[stub] run_executor gagal" in out, out[-600:])
    check("executor gagal -> state TETAP disimpan", "tools/save_state.sh" in out, out[-600:])
    check("loop selesai di exit-nya sendiri, bukan dibunuh bash -e",
          "[watch] selesai setelah 1 kali cek (exit 2)" in out, out[-600:])
    check("job tetap merah supaya kegagalan terlihat", p.returncode == 2, p.returncode)
    check("kunci hanya diteruskan ke executor", "kunci: kunci-uji" in out)
    # Audit 2026-10-01 #1: a hung connection froze the loop until GitHub's
    # 350-minute job limit, without saving that cycle's state.
    check("run_signal / run_executor / heartbeat dibatasi waktunya",
          out.count("[stub] timeout ") == 3, out[-600:])


def test_control_is_read_every_cycle():
    """Audit 2026-10-01 #2: the brake must not wait for the next job."""
    with open(os.path.join(ROOT, "tools", "refresh_state.sh"), encoding="utf-8") as fh:
        refresh = fh.read()
    fetch, show = refresh.find("git fetch"), refresh.find("control/executor.yaml")
    guard = refresh.find("git status --porcelain")
    check("refresh_state menyalin control/executor.yaml dari origin SEBELUM penjaga reset",
          0 <= fetch < show < guard, (fetch, show, guard))
    check("salinan itu di-gitignore (working tree tetap bersih untuk rebase)",
          ".mex_control_origin.yaml" in open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read())
    with open(os.path.join(WF, "control.yml"), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    on = doc.get("on") or doc.get(True)          # YAML 1.1: bare `on` is True
    opts = on["workflow_dispatch"]["inputs"]["mode"]["options"]
    from mex.control import MODES
    check("control.yml menawarkan semua mode + 'tetap'", set(opts) == set(MODES) | {"tetap"}, opts)


def test_save_control_pushes():
    bash, git = shutil.which("bash"), shutil.which("git")
    if not (bash and git):
        print("  SKIP  bash/git tidak ada")
        return
    import tempfile
    work = tempfile.mkdtemp()
    try:
        origin, clone = os.path.join(work, "origin.git"), os.path.join(work, "clone")
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GITHUB_REF_NAME="main",
                   GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")

        def sh(*a, cwd=None, extra=None):
            return subprocess.run(list(a), cwd=cwd, env={**env, **(extra or {})}, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=60)
        sh(git, "init", "-q", "--bare", "-b", "main", origin)
        sh(git, "clone", "-q", origin, clone)
        for rel in ("mex/__init__.py", "mex/compat.py", "mex/control.py", "tools/set_control.py", "tools/save_control.sh"):
            os.makedirs(os.path.join(clone, os.path.dirname(rel)), exist_ok=True)
            shutil.copy(os.path.join(ROOT, rel), os.path.join(clone, rel))
        sh(git, "checkout", "-q", "-b", "main", cwd=clone)
        sh(git, "add", "-A", cwd=clone)
        sh(git, "commit", "-q", "-m", "init", cwd=clone)
        sh(git, "push", "-q", "origin", "main", cwd=clone)
        p = sh(bash, "tools/save_control.sh", cwd=clone, extra={"MODE": "manage", "RESET": "true"})
        shown = sh(git, "--git-dir", origin, "show", "main:control/executor.yaml").stdout
        check("save_control.sh mendorong mode baru ke origin",
              p.returncode == 0 and 'mode: "manage"' in shown and 'breaker_reset: "reset-' in shown,
              (p.returncode, p.stdout[-300:], p.stderr[-300:], shown[-200:]))
        p = sh(bash, "tools/save_control.sh", cwd=clone, extra={"MODE": "liev", "RESET": "false"})
        check("save_control.sh menolak mode ngawur (merah, tidak ada commit)",
              p.returncode == 2 and 'mode: "manage"' in
              sh(git, "--git-dir", origin, "show", "main:control/executor.yaml").stdout, p.returncode)
    finally:
        shutil.rmtree(work, ignore_errors=True)


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


def test_secret_name_matches_config():
    # On 2026-10-01 the secret was recreated under a new name while signal.yml
    # still mapped the old one: the executor got an empty key.
    from mex.config import load
    want = load()["execution"]["agent_secret"]
    with open(os.path.join(WF, "signal.yml"), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    mapped = [step.get("env", {}).get("HL_AGENT_KEY") for job in doc["jobs"].values()
              for step in job.get("steps", []) if "HL_AGENT_KEY" in step.get("env", {})]
    check("signal.yml memetakan HL_AGENT_KEY dari secret execution.agent_secret",
          mapped == ["${{ secrets.%s }}" % want], (mapped, want))


if __name__ == "__main__":
    print("test_workflow.py")
    for t in (test_loop_survives_failures, test_every_command_guarded,
              test_secret_name_matches_config, test_control_is_read_every_cycle,
              test_save_control_pushes):
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    sys.exit(1 if FAIL else 0)
