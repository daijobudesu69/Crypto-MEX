"""The executor's prices and sizes, pushed through the REAL Hyperliquid SDK.

Offline: builds and signs order wires with a throwaway key; nothing is sent.
Hyperliquid rejects a price that breaks its tick rule, and the SDK refuses any
float that is not exact at 8 decimals (float_to_wire). Either would surface on
the first live signal -- with money on it -- so both are exercised here for all
13 coins at today's prices and at 10x and 1/10x of them.

The SDK's eth-account dependency loads native code that Windows Application
Control blocks on the author's machine, so outside CI a failed import is a skip.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mex.compat  # noqa: F401,E402

from mex.hl_client import (ENTRY_SLIPPAGE, KIND_STOP, STOP_SLIPPAGE, entry_cloid,  # noqa: E402
                           fresh_cloid, round_px, round_sz_down, round_sz_up)

PASS, FAIL = [], []

# szDecimals and mark price from Hyperliquid meta on 2026-09-29
COINS = {"ETH": (4, 2739.2), "SOL": (2, 120.22), "DOGE": (0, 0.095803), "SUI": (1, 1.1756),
         "LINK": (1, 15.39), "XRP": (0, 1.5168), "kSHIB": (0, 0.005858), "DOT": (1, 1.2148),
         "NEAR": (1, 4.8628), "TAO": (3, 314.105), "ENA": (0, 0.25435), "MNT": (1, 0.67121),
         "HYPE": (2, 88.438)}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + str(detail)}")


def main():
    try:
        import eth_account
        from hyperliquid.utils.signing import (order_request_to_order_wire,
                                               order_wires_to_order_action, sign_l1_action)
        from hyperliquid.utils.types import Cloid
    except Exception as e:  # noqa: BLE001
        if os.environ.get("CI"):
            print(f"SDK Hyperliquid wajib ada di CI: {type(e).__name__}: {e}")
            sys.exit(1)
        print(f"SKIP: SDK tidak bisa dimuat di mesin ini ({type(e).__name__})")
        sys.exit(0)

    wallet = eth_account.Account.create()
    bad = []
    for coin, (dec, mark) in COINS.items():
        for mid in (mark, mark * 10, mark / 10):
            for side in (1, -1):
                r = mid * 0.0365
                sz = round_sz_down(34.9 / mid, dec) or round_sz_up(10 / mid, dec)
                entry_limit = round_px(mid * (1 + side * ENTRY_SLIPPAGE), dec)
                trig = round_px(mid - side * r, dec)
                stop_limit = round_px(trig * (1 - side * STOP_SLIPPAGE), dec)
                cloids = [entry_cloid(coin, f"20260930T0400-{side}"),
                          fresh_cloid(KIND_STOP, coin, f"20260930T0400-{side}")]
                orders = [
                    {"coin": coin, "is_buy": side > 0, "sz": sz, "limit_px": entry_limit,
                     "order_type": {"limit": {"tif": "Ioc"}}, "reduce_only": False},
                    {"coin": coin, "is_buy": side < 0, "sz": sz, "limit_px": stop_limit,
                     "order_type": {"trigger": {"triggerPx": trig, "isMarket": True, "tpsl": "sl"}},
                     "reduce_only": True},
                ]
                try:
                    for o, c in zip(orders, cloids):
                        o["cloid"] = Cloid.from_str(c)
                    wires = [order_request_to_order_wire(o, 0) for o in orders]
                    if [w.get("c") for w in wires] != cloids:
                        bad.append((coin, mid, side, "cloid tidak terbawa ke wire"))
                    sign_l1_action(wallet, order_wires_to_order_action(wires), None, 1, None, True)
                    for px in (entry_limit, trig, stop_limit):
                        sig = len(f"{px:.10g}".replace(".", "").lstrip("0-"))
                        decs = len(f"{px:.10f}".rstrip("0").split(".")[1])
                        if sig > 5 and px != int(px) or decs > 6 - dec:
                            bad.append((coin, mid, px, "tick"))
                except Exception as e:  # noqa: BLE001
                    bad.append((coin, mid, side, f"{type(e).__name__}: {e}"))
    check("13 koin x 3 skala harga x 2 arah: order & stop (dengan cloid bot) lolos SDK + aturan tick",
          not bad, bad[:5])
    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    print("test_hl_sdk.py")
    main()
