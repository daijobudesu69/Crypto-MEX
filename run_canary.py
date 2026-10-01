"""Canary driver: .github/workflows/canary.yml, started by hand only.

    gh workflow run canary.yml --repo daijobudesu69/Crypto-MEX -f coin=ETH

Places two orders that cannot fill on the real account, moves one, cancels both
(mex/canary.py), and reports every step to Telegram. Exit 0 only when every
step passed. Touches no state/ file and no live position.

Environment: HL_AGENT_KEY (the API wallet's private key, GitHub secret).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402

from mex import canary, datafeed, notify  # noqa: E402
from mex.config import load  # noqa: E402
from mex.executor import Halt, coin_of, verify_agent  # noqa: E402
from run_executor import key_problem  # noqa: E402


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    coin = (argv[0] if argv else "ETH").strip()
    symbols = {coin_of(s): s for s in datafeed.SYMBOLS}
    if coin not in symbols:
        print(f"[canary] koin '{coin}' bukan salah satu dari {sorted(symbols)}")
        return 2
    ex = load()["execution"]
    key = os.environ.get("HL_AGENT_KEY", "").strip()
    problem = key_problem(key, ex.get("agent_secret") or "API wallet")
    if problem:
        print(f"[canary] BERHENTI: {problem}")
        notify.send(notify.alert_message("canary berhenti", problem))
        return 2
    from mex.hl_client import HLClient
    client = HLClient(key, ex["account_address"])
    del key
    try:
        verify_agent(client, ex, int(pd.Timestamp.now(tz="UTC").timestamp() * 1000))
    except Halt as e:
        print(f"[canary] BERHENTI: {e}")
        notify.send(notify.alert_message("canary berhenti", e))
        return 2
    print(f"[canary] {coin} -- akun {ex['account_address']}")
    rep = canary.run(client, coin, symbols[coin])
    text = canary.message(rep)
    notify.send(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(f"- {'✅' if ok else '❌'} {n}" + ("" if ok else f" — `{d[:300]}`")
                               for n, ok, d in rep.steps) + "\n")
    print(f"[canary] {'OK' if rep.ok else 'GAGAL'}")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
