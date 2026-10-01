"""Rewrite control/executor.yaml. Called by .github/workflows/control.yml.

    python tools/set_control.py --mode manage
    python tools/set_control.py --reset-breaker
    python tools/set_control.py --mode live --reset-breaker

--mode tetap (or no --mode) keeps the current mode. Prints what changed, and
exits 2 on an unknown mode so the workflow run goes red instead of committing
nonsense.
"""
import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mex import control  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="tetap")
    ap.add_argument("--reset-breaker", action="store_true")
    a = ap.parse_args(argv)

    mode, reset, problem = control.read(paths=(control.PATH,), env={})
    if problem:
        print(f"[control] file lama bermasalah: {problem}")
    new_mode = mode if a.mode.strip().lower() in ("", "tetap") else a.mode.strip().lower()
    if new_mode not in control.MODES:
        print(f"[control] mode '{a.mode}' tidak dikenal, pilih {', '.join(control.MODES)}")
        return 2
    new_reset = reset
    if a.reset_breaker:
        new_reset = f"reset-{dt.datetime.now(dt.timezone.utc):%Y%m%d-%H%M%S}"
    os.makedirs(os.path.dirname(control.PATH), exist_ok=True)
    with open(control.PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(control.render(new_mode, new_reset))
    print(f"[control] mode {mode} -> {new_mode}"
          + (f"; breaker_reset {reset or '(kosong)'} -> {new_reset}" if new_reset != reset else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
