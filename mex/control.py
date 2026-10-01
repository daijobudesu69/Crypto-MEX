"""Executor controls (mode, circuit-breaker reset), read on EVERY watcher cycle.

They used to be repo variables (MEX_EXEC_MODE, MEX_BREAKER_RESET). GitHub reads
a variable once, when the job starts, and a watcher job lives ~5.5 hours -- so
the emergency brake (`manage` / `off`) took up to 5.5 hours to engage. Audit
2026-10-01 #2.

The controls now live in control/executor.yaml on main. The watcher hard-resets
to origin/main every 10 minutes (tools/refresh_state.sh), and that script also
copies origin's version of this file to CACHE even when the reset itself is
skipped, so a change is picked up within one cycle. Change it with:

    gh workflow run control.yml --repo daijobudesu69/Crypto-MEX -f mode=manage
    gh workflow run control.yml --repo daijobudesu69/Crypto-MEX -f reset_breaker=true

Without the file (old checkouts, tests) the repo variables still apply.
"""
import os

import yaml

MODES = ("off", "dry", "manage", "live")
PATH = "control/executor.yaml"
# Written by tools/refresh_state.sh from origin/main; gitignored.
CACHE = ".mex_control_origin.yaml"
# A broken control file must not stop the bot protecting live positions, and
# must not open new ones: dry does exactly that.
FALLBACK = "dry"


def read(paths=(CACHE, PATH), env=None) -> tuple[str, str, str | None]:
    """(mode, breaker_reset, problem). problem is None when everything is valid."""
    env = os.environ if env is None else env
    path = next((p for p in paths if os.path.exists(p)), None)
    if path is None:
        mode = (env.get("MEX_EXEC_MODE") or "dry").strip().lower() or "dry"
        reset = (env.get("MEX_BREAKER_RESET") or "").strip()
        if mode not in MODES:
            return FALLBACK, reset, (f"MEX_EXEC_MODE '{mode}' tidak dikenal (pilih "
                                     f"{', '.join(MODES)}); dipakai '{FALLBACK}'")
        return mode, reset, None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        if not isinstance(doc, dict):
            raise ValueError("isinya bukan pasangan kunci: nilai")
    except Exception as e:  # noqa: BLE001
        return FALLBACK, "", (f"{PATH} tidak bisa dibaca ({type(e).__name__}: {e}); "
                              f"dipakai mode '{FALLBACK}'")
    mode = doc.get("mode")
    # YAML 1.1 reads a bare `mode: off` as the boolean False. render() quotes
    # the value, but a hand edit may not -- and "off" is the one mode that
    # must never silently turn into something else.
    mode = "off" if mode is False else str(mode or "").strip().lower()
    reset = str(doc.get("breaker_reset") or "").strip()
    if mode not in MODES:
        return FALLBACK, reset, (f"mode '{mode}' di {PATH} tidak dikenal (pilih "
                                 f"{', '.join(MODES)}); dipakai '{FALLBACK}'")
    return mode, reset, None


def render(mode: str, breaker_reset: str) -> str:
    """The whole file. Rewritten, not patched, by tools/set_control.py."""
    if mode not in MODES:
        raise ValueError(f"mode '{mode}' tidak dikenal, pilih {MODES}")
    return f"""\
# Kendali executor MEX 3.0. Dibaca watcher SETIAP 10 menit, jadi perubahan
# berlaku paling lambat ~10 menit (bukan menunggu job berikutnya).
#
# Jangan edit manual kalau tidak perlu -- pakai perintah ini (jalan di PowerShell):
#   gh workflow run control.yml --repo daijobudesu69/Crypto-MEX -f mode=manage
#   gh workflow run control.yml --repo daijobudesu69/Crypto-MEX -f reset_breaker=true
#
# mode:
#   live    entry baru + jaga posisi
#   dry     tanpa entry baru (rencana dikirim ke Telegram); posisi live tetap dijaga
#   manage  rem darurat: tanpa entry baru; posisi live tetap dijaga
#   off     tidak melakukan apa-apa (stop di bursa tetap ada tapi tidak digeser)
# breaker_reset: nilai BARU apa pun = circuit breaker di-reset, puncak saldo =
#   saldo saat itu (juga dipakai setelah withdraw).
mode: "{mode}"
breaker_reset: "{breaker_reset}"
"""
