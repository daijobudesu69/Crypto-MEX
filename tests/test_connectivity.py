"""Network check: are the live data sources reachable from THIS machine?

Run on every push so a source that gets geo-blocked or renamed is discovered by
a red CI badge, not by a week of silent missing signals. Binance's own trading
API is checked too -- only to confirm it is still blocked, which is the reason
the mirror is used at all.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mex.compat  # noqa: F401,E402

import requests  # noqa: E402

from mex import datafeed  # noqa: E402

FAIL = []


def probe(name, url, expect_ok=True):
    try:
        r = requests.get(url, timeout=25, headers=datafeed.UA)
        ok = r.status_code == 200
        print(f"  {name:<28} HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  {name:<28} {type(e).__name__}")
    if expect_ok and not ok:
        FAIL.append(name)
    return ok


print("Jangkauan endpoint dari runner ini:")
probe("binance spot mirror", datafeed.BINANCE_SPOT + "?symbol=ETHUSDT&interval=4h&limit=2")
probe("gate.io perp", datafeed.GATE_FUTURES + "?contract=ETH_USDT&interval=4h&limit=2")
blocked = probe("binance fapi (harus GAGAL)",
                "https://fapi.binance.com/fapi/v1/ping", expect_ok=False)
if blocked:
    print("  CATATAN: fapi.binance.com ternyata BISA diakses dari runner ini.")
    print("           Kalau ini konsisten, pertimbangkan pindah ke data Binance perp asli")
    print("           supaya tracking error terhadap backtest hilang sama sekali.")

print("\nAmbil data lewat datafeed.fetch():")
try:
    feed = datafeed.fetch(limit=400)
    df = feed.df
    print(f"  sumber   : {feed.source}")
    print(f"  bar      : {len(df)}  ({df['ts'].iloc[0]} -> {df['ts'].iloc[-1]})")
    print(f"  close     : {df['close'].iloc[-1]:,.2f}")
    print("  sanity_check: lulus")
except Exception as e:  # noqa: BLE001
    print(f"  GAGAL: {type(e).__name__}: {e}")
    FAIL.append("datafeed.fetch")

print(f"\n{'GAGAL: ' + ', '.join(FAIL) if FAIL else 'semua sumber data terjangkau'}")
sys.exit(1 if FAIL else 0)
