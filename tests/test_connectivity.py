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


def probe(name, url, expect_ok=True, body=None):
    try:
        r = (requests.get(url, timeout=25, headers=datafeed.UA) if body is None
             else requests.post(url, json=body, timeout=25, headers=datafeed.UA))
        ok = r.status_code == 200
        print(f"  {name:<28} HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  {name:<28} {type(e).__name__}")
    if expect_ok and not ok:
        FAIL.append(name)
    return ok


print("Jangkauan endpoint dari runner ini:")
probe("hyperliquid (utama)", datafeed.HYPERLIQUID, body={"type": "meta"})
probe("binance spot mirror", datafeed.BINANCE_SPOT + "?symbol=ETHUSDT&interval=4h&limit=2")
probe("gate.io perp", datafeed.GATE_FUTURES + "?contract=ETH_USDT&interval=4h&limit=2")
blocked = probe("binance fapi (harus GAGAL)",
                "https://fapi.binance.com/fapi/v1/ping", expect_ok=False)
if blocked:
    print("  CATATAN: fapi.binance.com ternyata BISA diakses dari runner ini.")
    print("           Kalau ini konsisten, pertimbangkan pindah ke data Binance perp asli")
    print("           supaya tracking error terhadap backtest hilang sama sekali.")

print("\nAmbil data lewat datafeed.fetch(), tiap simbol:")
for sym in datafeed.SYMBOLS:
    try:
        feed = datafeed.fetch(limit=400, prefer="hyperliquid", symbol=sym)
        df = feed.df
        note = "" if feed.source == "hyperliquid" else "   <- FAILOVER, bukan sumber utama"
        print(f"  {sym:<13} {feed.source:<20} {len(df)} bar  "
              f"close {df['close'].iloc[-1]:.6g}{note}")
        if feed.source != "hyperliquid":
            FAIL.append(f"{sym} tidak dari hyperliquid")
    except Exception as e:  # noqa: BLE001
        print(f"  {sym:<13} GAGAL: {type(e).__name__}: {e}")
        FAIL.append(f"datafeed.fetch {sym}")

print("\nLeverage maks Hyperliquid vs mex/execution.py:")
try:
    from mex import execution
    meta = requests.post(datafeed.HYPERLIQUID, json={"type": "meta"}, timeout=25,
                         headers=datafeed.UA).json()
    live = {u["name"]: u["maxLeverage"] for u in meta["universe"] if not u.get("isDelisted")}
    for sym, known in execution.HL_MAX_LEVERAGE.items():
        coin = datafeed.INSTRUMENTS[sym]["hyperliquid"][0]
        if live.get(coin) != known:
            print(f"  {sym:<13} BERUBAH: tercatat {known}x, sekarang {live.get(coin)}x "
                  f"-> perbarui HL_MAX_LEVERAGE (harga likuidasi di pesan jadi salah)")
            FAIL.append(f"max leverage {sym}")
    print("  cocok" if not any(f.startswith("max leverage") for f in FAIL) else "")
except Exception as e:  # noqa: BLE001
    print(f"  GAGAL: {type(e).__name__}: {e}")
    FAIL.append("hyperliquid meta")

print(f"\n{'GAGAL: ' + ', '.join(FAIL) if FAIL else 'semua sumber data terjangkau'}")
sys.exit(1 if FAIL else 0)
