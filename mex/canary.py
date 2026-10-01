"""Canary: prove the real order path works from the GitHub runner, without a fill.

Every executor test so far runs against a fake exchange (tests/test_executor.py)
or signs order wires offline (tests/test_hl_sdk.py). Neither proves that
Hyperliquid ACCEPTS an order signed by the API wallet from a GitHub runner, nor
the exact shape of its answers -- and the executor's safety logic leans on
those shapes: a stop counts as placed only on {"resting": {"oid": ...}}, and the
bot recognises its own orders by the cloid that frontendOpenOrders echoes back.
Audit 2026-09-30 #6 / 2026-10-01 #5.

What it does, on one coin with no open position:

  1. post-only (ALO) limit BUY 30% below the mid, ~$12. ALO is rejected rather
     than filled if it would cross, so it cannot fill on placement.
  2. stop-market BUY triggered 30% ABOVE the mid -- the same order type and
     answer as the executor's stop. It is not reduce-only (a reduce-only order
     needs a position, which the canary must not open).
  3. both visible in the open orders with their cloids
  4. the stop moved (modify, new cloid) exactly like the executor trails it
  5. both cancelled; orderStatus-by-cloid answers for the cancelled ALO order

Cleanup runs whatever happens: every order carrying the canary's cloid prefix
on that coin is cancelled, and a position that should never exist (a 30% move
within seconds) is closed reduce-only at once and reported.
"""
from dataclasses import dataclass, field

from .hl_client import BOT_PREFIX, fresh_cloid, round_px, round_sz_up

KIND_CANARY = "0c"
PREFIX = (BOT_PREFIX + KIND_CANARY).lower()
NOTIONAL_USD = 12.0          # above Hyperliquid's $10 minimum, measured at the limit price
ALO_AWAY = 0.30              # buy limit this far below the mid
TRIGGER_AWAY = 0.30          # stop trigger this far above the mid
MODIFY_AWAY = 0.35           # ... then moved here


@dataclass
class Report:
    coin: str
    steps: list = field(default_factory=list)     # (name, ok, detail)

    def add(self, name, ok, detail=""):
        self.steps.append((name, bool(ok), str(detail)))
        print(f"  [{'OK' if ok else 'GAGAL'}] {name}" + (f" -- {detail}" if detail else ""))
        return ok

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s[1] for s in self.steps)


def _cl(sym, what):
    # `what` keeps the three cloids distinct even within one microsecond.
    return fresh_cloid(KIND_CANARY, sym, f"canary-{what}")


def _find(orders, cloid):
    return next((o for o in orders if str(o.get("cloid") or "").lower() == cloid.lower()), None)


def run(client, coin: str, symbol: str) -> Report:
    rep = Report(coin)
    if coin in client.positions():
        rep.add("koin tanpa posisi terbuka", False,
                f"ada posisi {coin} di akun; canary tidak mau menaruh order di koin ini")
        return rep
    rep.add("koin tanpa posisi terbuka", True)
    mid = client.mids()[coin]
    dec = client.sz_decimals(coin)
    alo_px = round_px(mid * (1 - ALO_AWAY), dec)
    sz = round_sz_up(NOTIONAL_USD / alo_px, dec)
    alo_cl, stop_cl, mod_cl = _cl(symbol, "alo"), _cl(symbol, "stop"), _cl(symbol, "modify")
    try:
        r = client.place_alo(coin, True, sz, alo_px, cloid=alo_cl)
        rep.add(f"ALO beli {sz:g} {coin} @ {alo_px:g} (mid {mid:g}) diterima, jawaban 'resting'",
                "resting" in r, r)

        trig = mid * (1 + TRIGGER_AWAY)
        r = client.place_stop(coin, True, sz, trig, cloid=stop_cl, reduce_only=False)
        # The line the executor depends on: a stop counts as placed only on this answer.
        rep.add(f"stop-market (trigger {round_px(trig, dec):g}) diterima, jawaban 'resting'",
                "resting" in r, r)
        stop_oid = int(r["resting"]["oid"]) if "resting" in r else None

        book = client.open_orders()
        a, s = _find(book, alo_cl), _find(book, stop_cl)
        rep.add("ALO terlihat di open orders dengan cloid-nya", a is not None,
                "" if a else f"{len(book)} order, cloid tidak ditemukan")
        rep.add("stop terlihat di open orders: trigger + cloid-nya", s is not None and s.get("isTrigger"),
                "" if s else f"{len(book)} order, cloid tidak ditemukan")

        if stop_oid is not None:
            new_trig = mid * (1 + MODIFY_AWAY)
            r = client.modify_stop(stop_oid, coin, True, sz, new_trig, cloid=mod_cl, reduce_only=False)
            rep.add(f"stop digeser ke {round_px(new_trig, dec):g} dengan cloid baru (seperti trailing)",
                    "error" not in r, r)
            m = _find(client.open_orders(), mod_cl)
            rep.add("stop hasil geser terlihat dengan cloid & trigger baru",
                    m is not None and abs(float(m.get("triggerPx", 0)) - round_px(new_trig, dec))
                    < 10 ** -(6 - dec), m)
    except Exception as e:  # noqa: BLE001
        rep.add("langkah canary berjalan tanpa error", False, f"{type(e).__name__}: {e}")
    finally:
        _cleanup(client, coin, rep)
    try:
        st = client.order_by_cloid(alo_cl)
        status = ((st or {}).get("order") or {}).get("status")
        rep.add("orderStatus via cloid (dipakai adopsi posisi) menjawab 'canceled'",
                (st or {}).get("status") == "order" and status == "canceled", st)
    except Exception as e:  # noqa: BLE001
        rep.add("orderStatus via cloid (dipakai adopsi posisi) menjawab", False,
                f"{type(e).__name__}: {e}")
    return rep


def _cleanup(client, coin, rep):
    """Cancel everything the canary may have left; close a position it never should have made."""
    failed = []
    try:
        for o in client.open_orders():
            if o.get("coin") == coin and str(o.get("cloid") or "").lower().startswith(PREFIX):
                r = client.cancel(coin, int(o["oid"]))
                if "error" in r:
                    failed.append((o["oid"], r))
        left = [o for o in client.open_orders()
                if o.get("coin") == coin and str(o.get("cloid") or "").lower().startswith(PREFIX)]
        rep.add("semua order canary dibatalkan, tidak ada yang tersisa", not left and not failed,
                left or failed)
    except Exception as e:  # noqa: BLE001
        rep.add("semua order canary dibatalkan", False,
                f"{type(e).__name__}: {e} -- CEK OPEN ORDERS {coin} DI HYPERLIQUID")
    try:
        p = client.positions().get(coin)
        if p:
            r = client.market(coin, p["szi"] < 0, abs(p["szi"]), client.mids()[coin],
                              reduce_only=True, cloid=fresh_cloid(KIND_CANARY, coin, "close"))
            rep.add("tidak ada order canary yang terisi", False,
                    f"posisi {p['szi']:g} {coin} muncul dan langsung ditutup: {r}")
        else:
            rep.add("tidak ada order canary yang terisi (tidak ada posisi)", True)
    except Exception as e:  # noqa: BLE001
        rep.add("cek posisi setelah canary", False, f"{type(e).__name__}: {e} -- CEK POSISI {coin}")


def message(rep: Report) -> str:
    from .notify import esc
    head = ("✅ <b>CANARY OK</b>: jalur order Hyperliquid dari GitHub terbukti"
            if rep.ok else "🚨 <b>CANARY GAGAL</b>: jangan set mode live dulu")
    lines = [f"{'✅' if ok else '❌'} {esc(name)}" + ("" if ok else f"\n   <code>{esc(d[:300])}</code>")
             for name, ok, d in rep.steps]
    return f"{head} ({esc(rep.coin)})\n\n" + "\n".join(lines)
