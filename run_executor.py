"""Executor driver. Runs right after run_signal.py in the watcher loop.

Reads the strategy state run_signal.py just wrote, mirrors it onto the
Hyperliquid account (mex/executor.py), keeps its own state in state/live.json
and logs every action to state/live_trades.csv.

Environment:
  MEX_EXEC_MODE      off | dry | manage | live   (default dry; repo variable)
  HL_AGENT_KEY       private key of the API wallet (GitHub secret, never logged)
  MEX_BREAKER_RESET  any new value re-arms the circuit breaker (repo variable)
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402

from mex import ledger, notify  # noqa: E402
from mex.config import load  # noqa: E402
from mex.executor import REALERT, Executor, Halt, empty_state  # noqa: E402

STRATEGY = "state/position.json"
LIVE = "state/live.json"
LIVE_TRADES = "state/live_trades.csv"
# Throttle record for the one case live.json itself cannot hold it: live.json is
# unreadable. Overwriting it would throw away the only record of open positions.
FALLBACK_ALERTS = "state/live_alerts.json"
LIVE_COLS = ["logged_at_utc", "mode", "symbol", "action", "signal_id", "side", "size",
             "entry_px", "exit_px", "stop_px", "balance", "pnl_usd", "result_R", "reason"]
# Live-trading messages are retried like signals are, but a day-old one is noise.
OUTBOX_TTL = pd.Timedelta("24h")


def _throttled(live, now, key, text):
    """The watcher runs every 10 minutes; a standing failure must not page 144x a day."""
    alerts = live.setdefault("alerts", {})
    last = alerts.get(key)
    if last and now - pd.Timestamp(last) < REALERT:
        print("[exec] alert yang sama sudah dikirim < 24 jam lalu, tidak diulang")
        return
    if notify.send(text) or not notify.configured():
        alerts[key] = now.isoformat()
        ledger.write_json(LIVE, live)


def _deliver(live, now, events):
    """Send this run's messages plus any left over; keep what failed for the next run.

    A LIVE ENTRY, or a "position without a stop" alarm, lost to one Telegram
    timeout is exactly the message that must not be lost.
    """
    queue = list(live.get("outbox") or []) + [
        {"text": ev["text"], "queued_at": now.isoformat()} for ev in events]
    keep = []
    for m in queue:
        if now - pd.Timestamp(m["queued_at"]) > OUTBOX_TTL:
            print("[exec] pesan > 24 jam tidak terkirim, dibuang")
            continue
        if notify.send(m["text"]) or not notify.configured():
            continue
        keep.append(m)
    live["outbox"] = keep
    if keep:
        print(f"[exec] {len(keep)} pesan gagal terkirim, dicoba lagi run berikutnya")


def _save(res, now):
    _deliver(res.live, now, res.events)
    ledger.write_json(LIVE, res.live)
    for row in res.rows:
        ledger._append(LIVE_TRADES, LIVE_COLS, row)


def _corrupt(now, e) -> int:
    alerts = {}
    try:
        alerts = ledger.read_json(FALLBACK_ALERTS, {}) or {}
    except ledger.StateCorrupt:
        pass
    last = alerts.get("live_corrupt")
    print(f"[exec] BERHENTI: {e}")
    if not (last and now - pd.Timestamp(last) < pd.Timedelta("1h")):
        if notify.send(notify.alert_message(
                "executor berhenti: state/live.json rusak",
                f"{e} -- posisi live TIDAK dijaga sampai file ini diperbaiki; cek manual")) \
                or not notify.configured():
            alerts["live_corrupt"] = now.isoformat()
            ledger.write_json(FALLBACK_ALERTS, alerts)
    return 2


def key_problem(key: str) -> str | None:
    """Why the secret cannot be an API-wallet private key, or None if it can.

    Describes the value only by its shape -- never echoes any part of it. On
    2026-09-30 the secret held a 20-byte value (an address) and eth_account's
    error said only "must be exactly 32 bytes"; this says what to do instead.
    """
    h = key[2:] if key.lower().startswith("0x") else key
    if len(h) == 64 and all(c in "0123456789abcdefABCDEF" for c in h):
        return None
    if len(h) == 40 and all(c in "0123456789abcdefABCDEF" for c in h):
        return ("secret HYPERLIQUID_MEX_BOT_WALLET berisi sebuah ALAMAT wallet (40 karakter "
                "hex), bukan private key API wallet (64 karakter hex). Simpan private key "
                "API wallet MEX.bot ke secret itu; kalau sudah tidak tersimpan, buat API "
                "wallet baru.")
    return (f"secret HYPERLIQUID_MEX_BOT_WALLET bukan private key yang sah: panjangnya "
            f"{len(h)} karakter, seharusnya 64 karakter hex (boleh diawali 0x).")


def main() -> int:
    cfg = load()
    ex = cfg.get("execution")
    mode = os.environ.get("MEX_EXEC_MODE", "dry").strip().lower() or "dry"
    now = pd.Timestamp.now(tz="UTC")
    if not ex:
        print(f"[exec] mode={mode}, tidak ada yang dilakukan")
        return 0
    try:
        live = ledger.read_json(LIVE, None) or empty_state()
    except ledger.StateCorrupt as e:
        return _corrupt(now, e)
    if mode == "off":
        held = [s for s, t in (live.get("symbols") or {}).items()
                if t.get("status") in ("open", "entering")]
        if held:
            _throttled(live, now, "off-with-positions",
                       notify.alert_message("executor mode off",
                                            f"posisi live {', '.join(held)} tidak dijaga: stop "
                                            f"tetap di bursa tapi tidak digeser, exit strategi "
                                            f"tidak dieksekusi"))
        print(f"[exec] mode={mode}, tidak ada yang dilakukan")
        return 0
    key = os.environ.get("HL_AGENT_KEY", "").strip()
    if not key:
        print("[exec] HL_AGENT_KEY kosong -- secret HYPERLIQUID_MEX_BOT_WALLET belum "
              "dipetakan ke job ini")
        return 1

    problem = key_problem(key)
    if problem:
        del key
        print(f"[exec] BERHENTI: {problem}")
        _throttled(live, now, "badkey", notify.alert_message("executor berhenti", problem))
        ledger.write_json(LIVE, live)
        return 2

    strategy = ledger.read_json(STRATEGY, {})
    runner = None
    try:
        from mex.hl_client import HLClient
        client = HLClient(key, ex["account_address"])
        del key
        runner = Executor(client, ex, cfg["params"], mode, now,
                          persist=lambda st: ledger.write_json(LIVE, st),
                          breaker_reset=os.environ.get("MEX_BREAKER_RESET", ""))
        res = runner.run(strategy, live)
    except Halt as e:
        # Deliberately loud and deliberately inert: nothing was sent.
        print(f"[exec] BERHENTI: {e}")
        _throttled(live, now, f"halt:{e}", notify.alert_message("executor berhenti", e))
        return 2
    except Exception as e:  # noqa: BLE001
        # Type and message only. A requests error can embed a URL; it never
        # embeds the key, which lives only inside the eth_account object.
        print(f"[exec] ERROR: {type(e).__name__}: {e}")
        print(traceback.format_exc())
        # Whatever already happened in this run -- an order sent, a stop moved --
        # is still recorded and announced.
        if runner is not None and runner.res is not None:
            _save(runner.res, now)
        _throttled(live, now, f"error:{type(e).__name__}",
                   notify.alert_message("executor error", f"{type(e).__name__}: {e}"))
        return 1

    _save(res, now)
    opens = [s for s, t in res.live["symbols"].items() if t["status"] == "open"]
    print(f"[exec] mode={mode} selesai: {len(res.rows)} aksi, "
          f"{len(opens)} posisi live terbuka {opens if opens else ''}"
          + (f", simbol error: {','.join(res.errors)}" if res.errors else ""))
    return 1 if res.errors else 0


if __name__ == "__main__":
    sys.exit(main())
