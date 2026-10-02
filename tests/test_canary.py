"""Offline tests for mex/canary.py: the one script that places real orders by hand.

The canary exists to find out what the real exchange answers, so the tests
mostly check that it REPORTS honestly (a non-'resting' stop answer is a red
canary, not a crash) and that it ALWAYS cleans up -- after an exception, a
rejected order, or a fill that should never happen.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mex.compat  # noqa: F401,E402

from mex import canary  # noqa: E402
from mex.hl_client import is_bot_cloid, round_px  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + str(detail)}")


class FakeHL:
    """Resting orders only; nothing fills unless `fill_alo` says so."""

    def __init__(self, mid=2700.0, dec=4):
        self.mid, self.dec = mid, dec
        self.orders = {}
        self.pos = {}
        self.calls = []
        self.next_oid = 1
        self.stop_reply = None
        self.boom = {}
        self.fill_alo = False
        self.cancel_fails = False

    def _raise(self, name):
        if name in self.boom:
            raise self.boom[name]

    def positions(self):
        return {k: dict(v) for k, v in self.pos.items()}

    def mids(self):
        return {"ETH": self.mid}

    def sz_decimals(self, coin):
        return self.dec

    def _rest(self, coin, **o):
        oid = self.next_oid
        self.next_oid += 1
        self.orders[oid] = {"coin": coin, "oid": oid, **o}
        return oid

    def place_alo(self, coin, is_buy, sz, px, cloid=None):
        self.calls.append(("place_alo", coin, is_buy, sz, px, cloid))
        self._raise("place_alo")
        if sz * px < 10:
            return {"error": "Order must have minimum value of $10."}
        if self.fill_alo:
            self.pos[coin] = {"szi": sz, "entry_px": px}
            return {"filled": {"totalSz": str(sz), "avgPx": str(px), "oid": 99}}
        return {"resting": {"oid": self._rest(coin, cloid=cloid, isTrigger=False,
                                               triggerPx="0.0", limitPx=str(px), sz=str(sz))}}

    def place_stop(self, coin, is_buy, sz, trigger_px, cloid=None, reduce_only=True):
        self.calls.append(("place_stop", coin, is_buy, sz, trigger_px, cloid, reduce_only))
        self._raise("place_stop")
        oid = self._rest(coin, cloid=cloid, isTrigger=True,
                         triggerPx=str(round_px(trigger_px, self.dec)), sz=str(sz))
        return self.stop_reply if self.stop_reply is not None else {"resting": {"oid": oid}}

    def modify_stop(self, oid, coin, is_buy, sz, trigger_px, cloid=None, reduce_only=True):
        self.calls.append(("modify_stop", oid, trigger_px, cloid, reduce_only))
        self._raise("modify_stop")
        if oid not in self.orders:
            return {"error": "Cannot modify canceled or filled order"}
        o = self.orders.pop(oid)
        new = self._rest(coin, **{**{k: v for k, v in o.items() if k not in ("coin", "oid")},
                                  "cloid": cloid, "triggerPx": str(round_px(trigger_px, self.dec))})
        return {"resting": {"oid": new}}

    def open_orders(self):
        self._raise("open_orders")
        return [dict(o) for o in self.orders.values()]

    def cancel(self, coin, oid):
        self.calls.append(("cancel", coin, oid))
        if self.cancel_fails:
            return {"error": "nope"}
        self.orders.pop(oid, None)
        return {"ok": "success"}

    def order_by_cloid(self, cloid):
        return {"status": "order", "order": {"status": "canceled", "order": {"cloid": cloid}}}

    def market(self, coin, is_buy, sz, mid, reduce_only=False, cloid=None):
        self.calls.append(("market", coin, is_buy, sz, reduce_only))
        if reduce_only:
            self.pos.pop(coin, None)
        return {"filled": {"totalSz": str(sz), "avgPx": str(mid), "oid": 7}}


def names(f):
    return [c[0] for c in f.calls]


def test_happy_path():
    f = FakeHL()
    rep = canary.run(f, "ETH", "ETHUSDT")
    check("semua langkah lulus", rep.ok, [s for s in rep.steps if not s[1]])
    alo = next(c for c in f.calls if c[0] == "place_alo")
    stop = next(c for c in f.calls if c[0] == "place_stop")
    check("ALO beli 30% di bawah mid, nilai >= $10 di harga limit",
          alo[2] is True and alo[4] <= 2700 * 0.7 + 1e-9 and alo[3] * alo[4] >= 10, alo)
    check("stop beli 30% DI ATAS mid dan tidak reduce-only (tidak butuh posisi)",
          stop[2] is True and stop[4] >= 2700 * 1.3 - 1e-6 and stop[6] is False, stop)
    check("semua cloid canary berawalan bot + jenis 0c",
          all(c.lower().startswith(canary.PREFIX) and is_bot_cloid(c)
              for c in (alo[5], stop[5])), (alo[5], stop[5]))
    check("tiga cloid berbeda (alo, stop, geser)",
          len({alo[5], stop[5], next(c for c in f.calls if c[0] == "modify_stop")[3]}) == 3)
    check("stop digeser dengan cloid baru, tetap tidak reduce-only",
          any(c[0] == "modify_stop" and c[4] is False for c in f.calls), f.calls)
    check("tidak ada order tersisa, tidak ada posisi", not f.orders and not f.pos, f.orders)
    check("tidak ada market order sama sekali", "market" not in names(f))
    text = canary.message(rep)
    check("pesan Telegram: CANARY OK", "CANARY OK" in text, text)


def test_unexpected_stop_answer_is_red_not_a_crash():
    f = FakeHL()
    f.stop_reply = {"ok": "waitingForTrigger"}
    rep = canary.run(f, "ETH", "ETHUSDT")
    bad = [s for s in rep.steps if not s[1]]
    check("jawaban stop selain 'resting' -> canary MERAH dengan jawabannya tercatat",
          not rep.ok and any("resting" in s[0] and "waitingForTrigger" in s[2] for s in bad), bad)
    check("... dan tetap dibersihkan", not f.orders, f.orders)
    check("pesan Telegram: CANARY GAGAL + jangan live", "jangan set mode live" in canary.message(rep))


def test_exception_still_cleans_up():
    f = FakeHL()
    f.boom["modify_stop"] = TimeoutError("read timed out")
    rep = canary.run(f, "ETH", "ETHUSDT")
    check("error di tengah -> merah, error tercatat",
          not rep.ok and any("TimeoutError" in s[2] for s in rep.steps), rep.steps)
    check("error di tengah -> kedua order tetap dibatalkan", not f.orders, f.orders)


def test_refuses_coin_with_position():
    f = FakeHL()
    f.pos["ETH"] = {"szi": 0.01, "entry_px": 2600.0}
    rep = canary.run(f, "ETH", "ETHUSDT")
    check("ada posisi di koin itu -> tidak menaruh order apa pun",
          not rep.ok and not f.calls, f.calls)


def test_fill_is_closed_and_reported():
    f = FakeHL()
    f.fill_alo = True
    rep = canary.run(f, "ETH", "ETHUSDT")
    check("order yang tak semestinya terisi -> posisi langsung ditutup reduce-only",
          not f.pos and any(c[0] == "market" and c[4] is True for c in f.calls), f.calls)
    check("... dan canary merah", not rep.ok)


def test_cancel_failure_is_loud():
    f = FakeHL()
    f.cancel_fails = True
    rep = canary.run(f, "ETH", "ETHUSDT")
    check("pembatalan gagal -> merah (order tersisa disebut)",
          not rep.ok and any("dibatalkan" in s[0] and not s[1] for s in rep.steps), rep.steps)


def test_driver_refuses_bad_input():
    import run_canary
    env = dict(os.environ)
    try:
        os.environ["HL_AGENT_KEY"] = ""
        check("koin di luar universe bot ditolak", run_canary.main(["BTC"]) == 2)
        check("tanpa kunci -> berhenti sebelum menyentuh bursa", run_canary.main(["ETH"]) == 2)
    finally:
        os.environ.clear()
        os.environ.update(env)


if __name__ == "__main__":
    print("test_canary.py")
    for t in (test_happy_path, test_unexpected_stop_answer_is_red_not_a_crash,
              test_exception_still_cleans_up, test_refuses_coin_with_position,
              test_fill_is_closed_and_reported, test_cancel_failure_is_loud,
              test_driver_refuses_bad_input):
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    sys.exit(1 if FAIL else 0)
