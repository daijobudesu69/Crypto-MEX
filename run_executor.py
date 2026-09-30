"""Executor driver. Runs right after run_signal.py in the watcher loop.

Reads the strategy state run_signal.py just wrote, mirrors it onto the
Hyperliquid account (mex/executor.py), keeps its own state in state/live.json
and logs every action to state/live_trades.csv.

Environment:
  MEX_EXEC_MODE   off | dry | manage | live   (default dry; repo variable)
  HL_AGENT_KEY    private key of the API wallet (GitHub secret, never logged)
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
LIVE_COLS = ["logged_at_utc", "mode", "symbol", "action", "signal_id", "side", "size",
             "entry_px", "exit_px", "stop_px", "balance", "pnl_usd", "result_R", "reason"]


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


def main() -> int:
    cfg = load()
    ex = cfg.get("execution")
    mode = os.environ.get("MEX_EXEC_MODE", "dry").strip().lower() or "dry"
    if not ex or mode == "off":
        print(f"[exec] mode={mode}, tidak ada yang dilakukan")
        return 0
    key = os.environ.get("HL_AGENT_KEY", "").strip()
    if not key:
        print("[exec] HL_AGENT_KEY kosong -- secret HYPERLIQUID_MEX_BOT_WALLET belum "
              "dipetakan ke job ini")
        return 1

    now = pd.Timestamp.now(tz="UTC")
    live = ledger.read_json(LIVE, None) or empty_state()
    strategy = ledger.read_json(STRATEGY, {})
    try:
        from mex.hl_client import HLClient
        client = HLClient(key, ex["account_address"])
        del key
        res = Executor(client, ex, cfg["params"], mode, now,
                       persist=lambda st: ledger.write_json(LIVE, st)).run(strategy, live)
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
        _throttled(live, now, f"error:{type(e).__name__}",
                   notify.alert_message("executor error", f"{type(e).__name__}: {e}"))
        return 1

    ledger.write_json(LIVE, res.live)
    for row in res.rows:
        ledger._append(LIVE_TRADES, LIVE_COLS, row)
    for ev in res.events:
        notify.send(ev["text"])
    opens = [s for s, t in res.live["symbols"].items() if t["status"] == "open"]
    print(f"[exec] mode={mode} selesai: {len(res.rows)} aksi, "
          f"{len(opens)} posisi live terbuka {opens if opens else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
