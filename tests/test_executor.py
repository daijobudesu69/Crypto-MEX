"""Offline tests for mex/executor.py against a fake Hyperliquid account.

Real money rides on this file, so every path that sends an order is driven here:
entry, the 1R stop at fill, trailing the stop per bar, exits on both sides,
crash recovery, and each way an entry must refuse to happen.
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402

from mex.executor import Executor, Halt, empty_state, verify_agent  # noqa: E402
from mex.hl_client import (KIND_STOP, fresh_cloid, ioc_px, is_bot_cloid,  # noqa: E402
                           order_status, round_px, round_sz_down, round_sz_up)
from mex.strategy import Params  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + str(detail)}")


ACCOUNT = "0x123bb2a1FE74395a57081d48077C28c9cA55a93B"
AGENT = "0x5dcd653c361737ee61cb5b4863162e97796696a4"
EX = {"venue": "hyperliquid", "margin_mode": "isolated", "leverage": 4, "capital_usd": 100,
      "account_address": ACCOUNT, "agent_address": AGENT}
# szDecimals from Hyperliquid meta on 2026-09-29
SZ_DEC = {"ETH": 4, "SOL": 2, "DOGE": 0, "SUI": 1, "LINK": 1, "XRP": 0, "kSHIB": 0,
          "DOT": 1, "NEAR": 1, "TAO": 3, "ENA": 0, "MNT": 1, "HYPE": 2}
NOW = pd.Timestamp("2026-09-30T04:05:00Z")


class FakeHL:
    """Just enough of HLClient to be an honest exchange."""

    def __init__(self, mids, balance=127.52):
        self.agent_address = AGENT
        self._agents = [{"name": "MEX.bot", "address": AGENT, "validUntil": 1806235408635}]
        self._mids = dict(mids)
        self.balance = balance
        self.pos = {}                      # coin -> dict
        self.orders = {}                   # oid -> dict
        self.calls = []
        self.next_oid = 100
        self.fail = set()                  # names of methods that must fail
        self.boom = {}                     # method -> exception it raises (network, ...)
        self.filled_cloids = set()

    def _maybe_raise(self, name):
        if name in self.boom:
            raise self.boom[name]

    def agents(self):
        return self._agents

    def sz_decimals(self, coin):
        return SZ_DEC[coin]

    def mids(self):
        self._maybe_raise("mids")
        return dict(self._mids)

    def usdc_balance(self):
        return self.balance

    def positions(self):
        return copy.deepcopy(self.pos)

    def stop_orders(self):
        self._maybe_raise("stop_orders")
        out = {}
        for oid, o in self.orders.items():
            out.setdefault(o["coin"], []).append(
                {"oid": oid, **{k: o[k] for k in ("trigger_px", "sz", "is_buy")},
                 "cloid": o.get("cloid")})
        return out

    def entry_filled(self, cloid):
        self.calls.append(("entry_filled", cloid))
        return cloid in self.filled_cloids

    def set_isolated(self, coin, lev):
        self.calls.append(("set_isolated", coin, lev))
        return {"error": "nope"} if "set_isolated" in self.fail else {"ok": True}

    def market(self, coin, is_buy, sz, mid, reduce_only=False, cloid=None):
        self.calls.append(("market", coin, is_buy, sz, reduce_only))
        self._maybe_raise("close" if reduce_only else "market")
        name = "close" if reduce_only else "market"
        if name in self.fail:
            return {"error": "IOC tidak terisi"}
        # Hyperliquid's $10 minimum, measured at the order's limit price
        # (the stricter reading; reduce-only closes are exempt).
        if not reduce_only and sz * ioc_px(mid, is_buy, SZ_DEC[coin]) < 10.0:
            return {"error": "Order must have minimum value of $10."}
        if cloid and not reduce_only:
            self.filled_cloids.add(cloid)
        px = self._mids[coin]
        if reduce_only:
            self.pos.pop(coin, None)
        else:
            self.pos[coin] = {"szi": sz if is_buy else -sz, "entry_px": px,
                              "isolated": True, "margin_used": sz * px / 4}
        return {"filled": {"totalSz": str(sz), "avgPx": str(px), "oid": 1}}

    def place_stop(self, coin, is_buy, sz, trigger_px, cloid=None):
        self.calls.append(("place_stop", coin, is_buy, sz, round_px(trigger_px, SZ_DEC[coin])))
        self._maybe_raise("place_stop")
        if "place_stop" in self.fail:
            return {"error": "Order has invalid price"}
        oid = self.next_oid
        self.next_oid += 1
        self.orders[oid] = {"coin": coin, "trigger_px": round_px(trigger_px, SZ_DEC[coin]),
                            "sz": sz, "is_buy": is_buy, "cloid": cloid}
        return {"resting": {"oid": oid}}

    def modify_stop(self, oid, coin, is_buy, sz, trigger_px, cloid=None):
        self.calls.append(("modify_stop", coin, round_px(trigger_px, SZ_DEC[coin])))
        if "modify_stop" in self.fail:
            return {"error": "Order <b>rejected</b> & gone"}
        self.orders[oid].update(trigger_px=round_px(trigger_px, SZ_DEC[coin]), sz=sz,
                                cloid=cloid or self.orders[oid].get("cloid"))
        return {"ok": "success"}

    def cancel(self, coin, oid):
        self.calls.append(("cancel", coin, oid))
        self.orders.pop(oid, None)
        return {"ok": "success"}

    def fire_stop(self, coin):
        """The exchange-side stop triggers."""
        self.pos.pop(coin, None)
        for oid in [k for k, o in self.orders.items() if o["coin"] == coin]:
            self.orders.pop(oid)


def pending(sid, side=1, ref=120.0, r=4.38, expires="2026-09-30T08:00:00+00:00"):
    return {"signal_id": sid, "side": side, "ref_price": ref, "r_est": r,
            "zone_low": ref - 0.5 * r, "zone_high": ref + 0.5 * r, "expires_at": expires}


def strat(**slots):
    return {"schema": 2, "symbols": {s: v for s, v in slots.items()}}


def run(fake, strategy, live=None, mode="live", now=NOW):
    saved = []
    res = Executor(fake, EX, Params(), mode, now,
                   persist=lambda st: saved.append(copy.deepcopy(st))).run(strategy, live)
    return res, saved


def names(fake):
    return [c[0] for c in fake.calls]


# --------------------------------------------------------------------------- #
def test_rounding():
    check("harga 5 angka penting (ETH 4 desimal size -> 2 desimal harga)", round_px(2739.2345, 4) == 2739.2)
    check("harga kSHIB dipotong di 6 desimal (aturan Hyperliquid)", round_px(0.0058583, 0) == 0.005858)
    check("harga SOL", round_px(120.2249, 2) == 120.22)
    check("size dibulatkan ke bawah", round_sz_down(0.2869, 2) == 0.28)
    check("size dibulatkan ke atas untuk minimum $10", round_sz_up(0.0831, 2) == 0.09)
    check("respons error Hyperliquid terbaca",
          "error" in order_status({"status": "ok", "response": {"data": {"statuses": [{"error": "x"}]}}}))
    check("respons ditolak terbaca", "error" in order_status({"status": "err", "response": "bad"}))


def test_agent_verification():
    f = FakeHL({"SOL": 120.0})
    verify_agent(f, EX, int(NOW.timestamp() * 1000))
    check("agent benar lolos", True)
    for why, mutate in (
            ("kunci milik alamat lain", lambda f: setattr(f, "agent_address", "0x" + "1" * 40)),
            ("agent tidak terdaftar di akun", lambda f: setattr(f, "_agents", [])),
            ("agent sudah kedaluwarsa", lambda f: f._agents[0].update(validUntil=1))):
        g = FakeHL({"SOL": 120.0})
        mutate(g)
        try:
            run(g, strat(SOLUSDT={"pending": pending("S1")}))
            ok = False
        except Halt:
            ok = True
        check(f"berhenti total: {why}", ok and not g.calls, g.calls)


def test_mode_off_and_dry():
    f = FakeHL({"SOL": 120.0})
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}), mode="off")
    check("mode off: tidak menyentuh bursa sama sekali", not f.calls and not res.events)
    res, saved = run(f, strat(SOLUSDT={"pending": pending("S1")}), mode="dry")
    check("mode dry: tidak ada order", not f.calls, f.calls)
    check("mode dry: rencana dilaporkan ke Telegram",
          any("DRY-RUN" in e["text"] and "SOL" in e["text"] for e in res.events))
    check("mode dry: sinyal ditandai supaya tidak dilaporkan ulang", "S1" in res.live["handled"])
    res2, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}), live=res.live, mode="dry")
    check("mode dry: run berikutnya diam", not res2.events)


def test_live_entry_long():
    f = FakeHL({"SOL": 120.0})
    res, saved = run(f, strat(SOLUSDT={"pending": pending("S1", r=4.38)}))
    t = res.live["symbols"]["SOLUSDT"]
    check("urutan: isolated 4x -> market -> stop", names(f) == ["set_isolated", "market", "place_stop"], names(f))
    check("isolated 4x", f.calls[0] == ("set_isolated", "SOL", 4))
    # risk 1% of 127.52 = 1.2752; stop distance 4.38/120 = 3.65% -> order $34.94 -> 0.29 SOL
    check("ukuran: 1% saldo / jarak stop, dibulatkan ke bawah", f.calls[1][3] == 0.29, f.calls[1])
    check("stop 1R langsung dipasang, reduce-only jual", f.calls[2] == ("place_stop", "SOL", False, 0.29, 115.62), f.calls[2])
    check("status open, oid stop tercatat", t["status"] == "open" and t["stop_oid"] == 100)
    check("state 'entering' disimpan SEBELUM order dikirim",
          saved and saved[0]["symbols"]["SOLUSDT"]["status"] == "entering")
    check("notifikasi entry live", any("LIVE ENTRY SOLUSDT" in e["text"] for e in res.events))
    risk = 0.29 * 4.38
    check("risiko nyata <= 1% saldo", risk <= 0.01 * 127.52 + 1e-9, risk)
    res2, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}), live=res.live)
    check("run berikutnya tidak entry dua kali", names(f).count("market") == 1)


def test_short_and_min_order():
    f = FakeHL({"HYPE": 90.0})
    res, _ = run(f, strat(HYPEUSDT={"pending": pending("H1", side=-1, ref=90.0, r=12.15)}))
    mk = f.calls[1]
    check("short: market jual", mk[0] == "market" and mk[2] is False)
    check("order < $10 dinaikkan ke $10 (0.12 HYPE = $10.80)", mk[3] == 0.12 and mk[3] * 90 >= 10, mk)
    check("short: stop beli DI ATAS entry", f.calls[2][2] is True and f.calls[2][4] > 90.0, f.calls[2])


def test_entry_refusals():
    cases = [
        ("harga di luar zona", {"SOL": 125.0}, pending("S1"), NOW, {}, "di luar zona"),
        ("sinyal hangus", {"SOL": 120.0}, pending("S1"), pd.Timestamp("2026-09-30T09:00Z"), {}, "hangus"),
        ("sudah ada posisi manual", {"SOL": 120.0}, pending("S1"), NOW,
         {"SOL": {"szi": 1.0, "entry_px": 110.0, "isolated": False, "margin_used": 20}}, "sudah ada posisi"),
    ]
    for why, mids, pend, now, pos, needle in cases:
        f = FakeHL(mids)
        f.pos = pos
        res, _ = run(f, strat(SOLUSDT={"pending": pend}), now=now)
        orders = [c for c in f.calls if c[0] in ("market", "place_stop", "set_isolated")]
        check(f"tidak entry: {why}", not orders and "S1" in res.live["handled"], f.calls)
        check(f"alasan tercatat: {why}", any(needle in (r["reason"] or "") for r in res.rows),
              [r["reason"] for r in res.rows])
    f = FakeHL({"SOL": 120.0, "ETH": 2700.0}, balance=100.0)
    f.pos["ETH"] = {"szi": 0.1, "entry_px": 2700.0, "isolated": True, "margin_used": 98.0}
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}))
    check("tidak entry: margin kurang", "market" not in names(f)
          and any("margin kurang" in (r["reason"] or "") for r in res.rows), res.rows)
    f = FakeHL({"SOL": 120.0}, balance=3.0)
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}))
    check("tidak entry: saldo kecil membuat order $10 berisiko > 2x target", "market" not in names(f)
          and any("saldo terlalu kecil" in (r["reason"] or "") for r in res.rows), res.rows)
    f = FakeHL({"HYPE": 90.0}, balance=127.52)
    res, _ = run(f, strat(HYPEUSDT={"pending": pending("H1", ref=90.0, r=12.15)}))
    check("saldo normal: order dinaikkan ke $10 tetap jalan (risiko 1.06x)", "market" in names(f))


def test_margin_counts_entries_in_same_run():
    # Two signals at once, $10 balance: each order takes $5 margin at 4x, so the
    # second no longer fits once the first is counted.
    mids = {"SOL": 120.0, "ETH": 2700.0}
    f = FakeHL(mids, balance=10.0)
    s = strat(SOLUSDT={"pending": pending("S1", r=0.6)},
              ETHUSDT={"pending": pending("E1", ref=2700.0, r=13.5)})
    res, _ = run(f, s)
    entered = [c[1] for c in f.calls if c[0] == "market"]
    check("entry kedua di run yang sama memperhitungkan margin entry pertama",
          len(entered) == 1 and any("margin kurang" in (r["reason"] or "") for r in res.rows),
          (entered, [r["reason"] for r in res.rows]))


def test_manage_mode_keeps_signal_open():
    f = FakeHL({"SOL": 120.0})
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}), mode="manage")
    check("mode manage: tidak entry", "market" not in names(f))
    check("mode manage: sinyal TIDAK dihanguskan", "S1" not in res.live["handled"])
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}), live=res.live, mode="live")
    check("dinyalakan lagi dalam masa berlaku -> tetap entry", "market" in names(f))


def test_stop_follows_strategy_trail():
    f = FakeHL({"SOL": 120.0})
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}))
    f.calls.clear()
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}), live=res.live)
    check("selama candle entry: stop 1R dibiarkan", not f.calls, f.calls)
    pos = {"signal_id": "S1", "side": 1, "trail": 116.9}
    res, _ = run(f, strat(SOLUSDT={"position": pos}), live=res.live)
    check("setelah candle tutup: stop digeser ke trail strategi",
          ("modify_stop", "SOL", 116.9) in f.calls, f.calls)
    f.calls.clear()
    res, _ = run(f, strat(SOLUSDT={"position": pos}), live=res.live)
    check("trail sama: tidak ada panggilan ke bursa", not f.calls, f.calls)
    f.orders.clear()
    res, _ = run(f, strat(SOLUSDT={"position": pos}), live=res.live)
    check("stop hilang dari bursa: dipasang ulang", ("place_stop", "SOL", False, 0.29, 116.9) in f.calls, f.calls)
    f.place_stop("SOL", False, 0.29, 110.0, cloid=fresh_cloid(KIND_STOP, "SOLUSDT", "S1"))
    f.calls.clear()
    res, _ = run(f, strat(SOLUSDT={"position": pos}), live=res.live)
    check("stop ganda milik bot: yang ekstra dibatalkan", [c[0] for c in f.calls] == ["cancel"], f.calls)


def test_exits():
    f = FakeHL({"SOL": 120.0})
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}))
    pos = {"signal_id": "S1", "side": 1, "trail": 118.0}
    f.fire_stop("SOL")
    res, _ = run(f, strat(SOLUSDT={"position": pos}), live=res.live)
    t = res.live["symbols"]["SOLUSDT"]
    check("stop tereksekusi di bursa -> tercatat closed", t["status"] == "closed")
    check("exit dilaporkan dengan R", any("LIVE EXIT" in e["text"] and " R " in e["text"] for e in res.events))
    f.calls.clear()
    res, _ = run(f, strat(SOLUSDT={"position": pos}), live=res.live)
    check("sinyal yang sama tidak di-entry ulang", "market" not in names(f))

    f = FakeHL({"SOL": 120.0})
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}))
    f.calls.clear()
    res, _ = run(f, strat(SOLUSDT={"position": None, "pending": None}), live=res.live)
    check("strategi exit -> tutup market reduce-only + batalkan stop",
          f.calls[0][0] == "market" and f.calls[0][4] is True and f.calls[1][0] == "cancel", f.calls)
    check("posisi di bursa habis", "SOL" not in f.pos)

    f = FakeHL({"SOL": 120.0})
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}))
    f.calls.clear()
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S2")}), live=res.live)
    # entry_filled is a read: the bot first checks the account is not already S2's.
    sent = [n for n in names(f) if n != "entry_filled"]
    check("exit lalu sinyal baru di run yang sama -> tutup lalu entry baru",
          sent == ["market", "cancel", "set_isolated", "market", "place_stop"], names(f))


def test_crash_recovery():
    f = FakeHL({"SOL": 120.0})
    live = empty_state()
    live["symbols"]["SOLUSDT"] = {"signal_id": "S1", "status": "entering", "side": 1, "coin": "SOL",
                                  "size": 0.29, "entry_px": None, "stop_px": 115.62, "stop_oid": None,
                                  "r_est": 4.38, "balance_at_entry": 127.52}
    f.pos["SOL"] = {"szi": 0.29, "entry_px": 120.1, "isolated": True, "margin_used": 8.7}
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}), live=live)
    check("run terputus setelah fill: posisi dipulihkan + stop dipasang",
          res.live["symbols"]["SOLUSDT"]["status"] == "open" and "place_stop" in names(f), f.calls)
    check("tidak entry dua kali saat pemulihan", "market" not in names(f))

    g = FakeHL({"SOL": 120.0})
    live["symbols"]["SOLUSDT"]["status"] = "entering"
    res, _ = run(g, strat(SOLUSDT={"pending": pending("S1")}), live=live)
    check("run terputus sebelum fill: ditandai skipped, tidak entry ulang",
          res.live["symbols"]["SOLUSDT"]["status"] == "skipped" and "market" not in names(g), g.calls)


def test_failures_never_leave_naked_position():
    f = FakeHL({"SOL": 120.0})
    f.fail = {"place_stop"}
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}))
    check("stop gagal -> posisi langsung ditutup", "SOL" not in f.pos
          and res.live["symbols"]["SOLUSDT"]["status"] == "closed", f.calls)

    g = FakeHL({"SOL": 120.0})
    g.fail = {"place_stop", "close"}
    res, _ = run(g, strat(SOLUSDT={"pending": pending("S1")}))
    check("stop DAN penutupan gagal -> tetap 'open' + alarm", res.live["symbols"]["SOLUSDT"]["status"] == "open"
          and any("TANPA STOP" in e["text"] for e in res.events))
    g.fail = set()
    g.calls.clear()
    res, _ = run(g, strat(SOLUSDT={"pending": pending("S1")}), live=res.live)
    check("run berikutnya memasang stop", "place_stop" in names(g), g.calls)

    h = FakeHL({"SOL": 120.0})
    h.fail = {"set_isolated"}
    res, _ = run(h, strat(SOLUSDT={"pending": pending("S1")}))
    check("gagal set isolated -> tidak ada order", "market" not in names(h))
    h = FakeHL({"SOL": 120.0})
    h.fail = {"market"}
    res, _ = run(h, strat(SOLUSDT={"pending": pending("S1")}))
    check("market tidak terisi -> skipped, tanpa stop", "place_stop" not in names(h)
          and res.live["symbols"]["SOLUSDT"]["status"] == "skipped")


def test_missed_and_orphans():
    f = FakeHL({"SOL": 120.0, "ETH": 2700.0})
    res, _ = run(f, strat(SOLUSDT={"position": {"signal_id": "S9", "side": 1, "trail": 110.0}}))
    check("posisi strategi tanpa entry live -> tidak dikejar", "market" not in names(f)
          and any("terlewat" in (r["reason"] or "") for r in res.rows))
    f.pos["ETH"] = {"szi": 0.01, "entry_px": 2600.0, "isolated": False, "margin_used": 5}
    res, _ = run(f, strat(), live=res.live)
    check("posisi manual -> diperingatkan, tidak disentuh",
          any("tidak dibuka bot" in e["text"] for e in res.events) and "market" not in names(f))
    res2, _ = run(f, strat(), live=res.live, now=NOW + pd.Timedelta("1h"))
    check("peringatan yang sama tidak diulang dalam 24 jam", not res2.events)


def test_orders_carry_bot_cloid():
    f = FakeHL({"SOL": 120.0})
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}))
    t = res.live["symbols"]["SOLUSDT"]
    stop = next(iter(f.orders.values()))
    check("entry memakai cloid bot yang deterministik",
          is_bot_cloid(t["entry_cloid"]) and t["entry_cloid"] in f.filled_cloids)
    check("stop memakai cloid bot", is_bot_cloid(stop["cloid"]))


def test_symbol_error_does_not_stop_others():
    # ETH is processed before SOL, DOGE after it (datafeed.SYMBOLS order).
    f = FakeHL({"SOL": 120.0, "ETH": 4000.0, "DOGE": 0.25})
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}))
    real = f.sz_decimals

    def sz_dec(coin):
        if coin == "SOL":
            raise KeyError("SOL diganti nama")
        return real(coin)
    f.sz_decimals = sz_dec
    s = strat(ETHUSDT={"pending": pending("E1", ref=4000.0, r=120.0)},
              SOLUSDT={"position": {"signal_id": "S1", "side": 1, "trail": 117.0}},
              DOGEUSDT={"pending": pending("D1", ref=0.25, r=0.0075)})
    res, saved = run(f, s, live=res.live)
    check("simbol yang error tercatat, run tidak berhenti", res.errors == ["SOLUSDT"], res.errors)
    check("simbol SEBELUM yang error tetap entry dan tercatat",
          "ETH" in f.pos and any(r["action"] == "ENTRY" and r["symbol"] == "ETHUSDT" for r in res.rows))
    check("simbol SESUDAH yang error tetap diproses", "DOGE" in f.pos, list(f.pos))
    check("alert error menyebut simbolnya dan posisi live-nya",
          any("SOLUSDT" in e["text"] and "cek manual" in e["text"] for e in res.events))
    res2, _ = run(f, s, live=res.live, now=NOW + pd.Timedelta("30min"))
    check("alert error posisi live tidak diulang < 1 jam",
          not any("executor error" in e["text"] for e in res2.events))
    res3, _ = run(f, s, live=res2.live, now=NOW + pd.Timedelta("61min"))
    check("alert error posisi live diulang setelah 1 jam",
          any("executor error" in e["text"] for e in res3.events))


def test_dry_still_protects_live_positions():
    f = FakeHL({"SOL": 120.0})
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}))
    f.calls.clear()
    pos = {"signal_id": "S1", "side": 1, "trail": 117.0}
    res, _ = run(f, strat(SOLUSDT={"position": pos}), live=res.live, mode="dry")
    check("live -> dry: stop tetap digeser ke trail", ("modify_stop", "SOL", 117.0) in f.calls, f.calls)
    check("live -> dry: ada peringatan mode dry dengan posisi live",
          any("Mode dry" in e["text"] for e in res.events))
    res, _ = run(f, strat(SOLUSDT={}), live=res.live, mode="dry")
    check("live -> dry: exit strategi tetap ditutup di bursa",
          "SOL" not in f.pos and res.live["symbols"]["SOLUSDT"]["status"] == "closed")
    f.calls.clear()
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S2")}), live=res.live, mode="dry")
    check("mode dry tetap tidak membuka posisi baru", "market" not in names(f), f.calls)


def test_adopt_after_lost_state():
    f = FakeHL({"SOL": 120.0})
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}))
    f.calls.clear()
    pos = {"signal_id": "S1", "side": 1, "trail": 117.0, "r_usdt": 4.5}
    res2, _ = run(f, strat(SOLUSDT={"position": pos}), live=None)
    t = res2.live["symbols"].get("SOLUSDT") or {}
    check("live.json hilang: posisi bot diadopsi ulang", t.get("status") == "open", t)
    check("adopsi: stop bot lama dipakai dan digeser ke trail, tanpa entry baru",
          ("modify_stop", "SOL", 117.0) in f.calls and "market" not in names(f), f.calls)
    check("adopsi tercatat & diumumkan", any(r["action"] == "ADOPTED" for r in res2.rows)
          and any("diambil alih" in e["text"] for e in res2.events))

    g = FakeHL({"SOL": 120.0})
    res, _ = run(g, strat(SOLUSDT={"pending": pending("S1")}))
    g.calls.clear()
    res2, _ = run(g, strat(SOLUSDT={"pending": pending("S1")}), live=None)
    check("hilang saat masih pending: diadopsi, tidak entry dua kali",
          res2.live["symbols"]["SOLUSDT"]["status"] == "open" and "market" not in names(g), g.calls)

    h = FakeHL({"SOL": 120.0})
    h.pos["SOL"] = {"szi": 1.0, "entry_px": 110.0, "isolated": False, "margin_used": 20}
    res, _ = run(h, strat(SOLUSDT={"position": {"signal_id": "S9", "side": 1, "trail": 110.0}}))
    check("posisi manual (tanpa cloid entry bot) TIDAK diadopsi",
          "SOLUSDT" not in res.live["symbols"]
          and any("tidak dibuka bot" in e["text"] for e in res.events))
    h.calls.clear()
    run(h, strat(SOLUSDT={"position": {"signal_id": "S9", "side": 1, "trail": 110.0}}), live=res.live)
    check("pemeriksaan cloid tidak diulang tiap run untuk sinyal yang sudah ditangani",
          "entry_filled" not in names(h), h.calls)

    # Stale state: the record still shows trade S1 open, the account holds S2.
    k = FakeHL({"SOL": 120.0})
    res_old, _ = run(k, strat(SOLUSDT={"pending": pending("S1")}))
    stale = copy.deepcopy(res_old.live)
    k.fire_stop("SOL")
    res, _ = run(k, strat(SOLUSDT={"pending": pending("S2")}), live=res_old.live)
    k.calls.clear()
    res, _ = run(k, strat(SOLUSDT={"position": {"signal_id": "S2", "side": 1, "trail": 118.0}}),
                 live=stale)
    check("state basi: posisi S2 milik bot TIDAK ditutup sebagai 'exit S1'",
          "SOL" in k.pos and not any(c[0] == "market" for c in k.calls), k.calls)
    check("state basi: S1 ditutup di catatan, S2 diadopsi",
          res.live["symbols"]["SOLUSDT"]["signal_id"] == "S2"
          and res.live["symbols"]["SOLUSDT"]["status"] == "open")


def test_min_order_at_limit_price():
    # DOGE short, stop 14%: raised to $10. Exactly 40 DOGE is $10.00 at the mid
    # but $9.90 at the IOC limit -- rejected. The order must clear $10 at the limit.
    f = FakeHL({"DOGE": 0.25})
    res, _ = run(f, strat(DOGEUSDT={"pending": pending("D1", side=-1, ref=0.25, r=0.035)}))
    mk = next(c for c in f.calls if c[0] == "market")
    check("short minimum $10: ukuran dihitung di harga limit, order diterima",
          mk[3] == 41 and res.live["symbols"]["DOGEUSDT"]["status"] == "open",
          (mk, res.live["symbols"]))


def test_user_orders_untouched():
    f = FakeHL({"SOL": 120.0})
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}))
    f.place_stop("SOL", False, 0.29, 130.0)          # a take-profit placed by hand, no cloid
    user_oid = max(f.orders)
    f.calls.clear()
    res, _ = run(f, strat(SOLUSDT={"position": {"signal_id": "S1", "side": 1, "trail": 117.0}}),
                 live=res.live)
    check("order trigger manual tidak dibatalkan", user_oid in f.orders
          and ("cancel", "SOL", user_oid) not in f.calls, f.calls)
    check("order trigger manual tidak diubah jadi stop bot", f.orders[user_oid]["trigger_px"] == 130.0)
    f.fire_stop("SOL")
    f.place_stop("SOL", False, 0.29, 130.0)
    user_oid = max(f.orders)
    f.calls.clear()
    run(f, strat(SOLUSDT={}), live=res.live)
    check("posisi selesai: order manual tetap tidak disentuh",
          ("cancel", "SOL", user_oid) not in f.calls, f.calls)


def test_circuit_breaker():
    ex = {**EX, "max_drawdown_pct": 40}
    f = FakeHL({"SOL": 120.0, "ETH": 4000.0})

    def go(strategy, live=None, mode="live", now=NOW, reset=""):
        return Executor(f, ex, Params(), mode, now, breaker_reset=reset).run(strategy, live)

    res = go(strat())
    check("puncak saldo tercatat", res.live["peak_balance"] == 127.52)
    res = go(strat(SOLUSDT={"pending": pending("S1")}), res.live)
    f.balance = 80.0                                  # -37%: masih di bawah batas
    res = go(strat(SOLUSDT={"position": {"signal_id": "S1", "side": 1, "trail": 117.0}}), res.live)
    check("drawdown 37% < 40%: breaker belum aktif", res.live["breaker"] is None)
    f.balance = 76.0                                  # -40.4%
    res = go(strat(SOLUSDT={"position": {"signal_id": "S1", "side": 1, "trail": 118.0}},
                   ETHUSDT={"pending": pending("E1", ref=4000.0, r=120.0)}), res.live)
    check("drawdown 40.4%: breaker aktif + alert", res.live["breaker"] is not None
          and any("Circuit breaker AKTIF" in e["text"] for e in res.events))
    check("breaker: entry baru tidak dikirim, alasannya tercatat", "ETH" not in f.pos
          and any("circuit breaker" in (r["reason"] or "") for r in res.rows), res.rows)
    check("breaker: posisi terbuka tetap dijaga (stop digeser)",
          any(o["trigger_px"] == 118.0 for o in f.orders.values()), f.orders)
    f.balance = 90.0
    res = go(strat(ETHUSDT={"pending": pending("E2", ref=4000.0, r=120.0)}), res.live)
    check("breaker tidak mati sendiri walau saldo naik",
          res.live["breaker"] is not None and "ETH" not in f.pos)
    res = go(strat(ETHUSDT={"pending": pending("E3", ref=4000.0, r=120.0)}), res.live, reset="r1")
    check("reset: breaker mati, puncak = saldo sekarang, entry jalan lagi",
          res.live["breaker"] is None and res.live["peak_balance"] == 90.0 and "ETH" in f.pos)
    res = go(strat(), res.live, reset="r1")
    check("nilai reset yang sama tidak me-reset ulang", res.live["breaker_reset_seen"] == "r1"
          and not any("di-reset" in e["text"] for e in res.events))
    g = FakeHL({"SOL": 120.0})
    res = Executor(g, EX, Params(), "live", NOW).run(strat(), None)
    g.balance = 10.0
    res = Executor(g, EX, Params(), "live", NOW).run(
        strat(SOLUSDT={"pending": pending("S1", r=0.6)}), res.live)
    check("tanpa max_drawdown_pct di config: breaker tidak pernah aktif", res.live["breaker"] is None)


def test_alert_text_is_escaped():
    f = FakeHL({"SOL": 120.0})
    res, _ = run(f, strat(SOLUSDT={"pending": pending("S1")}))
    f.fail = {"modify_stop"}
    res, _ = run(f, strat(SOLUSDT={"position": {"signal_id": "S1", "side": 1, "trail": 117.0}}),
                 live=res.live)
    txt = " ".join(e["text"] for e in res.events)
    check("teks error bursa di-escape untuk HTML Telegram",
          "&lt;b&gt;rejected&lt;/b&gt; &amp; gone" in txt and "<b>rejected" not in txt, txt)


def test_driver_delivery_and_state():
    import json
    import shutil
    import tempfile
    import mex.hl_client as hl
    from mex import notify
    import run_executor

    work, cwd = tempfile.mkdtemp(), os.getcwd()
    env = dict(os.environ)
    real_client, real_send, real_conf = hl.HLClient, notify.send, notify.configured
    sent, up = [], {"ok": False}
    notify.send = lambda text: (sent.append(text) or True) if up["ok"] else False
    notify.configured = lambda: True
    try:
        os.chdir(work)
        os.makedirs("state")
        with open("state/position.json", "w", encoding="utf-8") as fh:
            json.dump(strat(SOLUSDT={"pending": pending("S1", expires="2099-01-01T00:00:00+00:00")}), fh)
        os.environ.update(MEX_EXEC_MODE="live", HL_AGENT_KEY="0x" + "ab" * 32)
        fake = FakeHL({"SOL": 120.0})
        hl.HLClient = lambda key, account: fake
        check("driver: entry dengan Telegram mati tetap jalan", run_executor.main() == 0)
        live = json.load(open("state/live.json", encoding="utf-8"))
        check("driver: pesan LIVE ENTRY yang gagal terkirim disimpan di outbox",
              any("LIVE ENTRY" in m["text"] for m in live.get("outbox", [])), live.get("outbox"))
        up["ok"] = True
        run_executor.main()
        live = json.load(open("state/live.json", encoding="utf-8"))
        check("driver: outbox terkirim di run berikutnya lalu kosong",
              any("LIVE ENTRY" in t for t in sent) and not live["outbox"], (sent, live["outbox"]))

        fake.boom["stop_orders"] = ConnectionError("timeout")
        check("driver: error jaringan saat membaca akun -> exit 1", run_executor.main() == 1)
        fake.boom.clear()

        os.environ["MEX_EXEC_MODE"] = "off"
        sent.clear()
        check("driver: mode off keluar 0", run_executor.main() == 0)
        check("driver: mode off dengan posisi live -> alert", any("mode off" in t for t in sent), sent)

        good = open("state/live.json", encoding="utf-8").read()
        with open("state/live.json", "w", encoding="utf-8") as fh:
            fh.write('{"symbols": {"SOLUSDT": ')
        os.environ["MEX_EXEC_MODE"] = "live"
        sent.clear()
        fake.calls.clear()
        check("driver: live.json rusak -> berhenti (exit 2)", run_executor.main() == 2)
        check("driver: live.json rusak -> alert, tidak ada order",
              any("live.json rusak" in t for t in sent) and not fake.calls, (sent, fake.calls))
        check("driver: live.json rusak TIDAK ditimpa",
              open("state/live.json", encoding="utf-8").read() == '{"symbols": {"SOLUSDT": ')
        run_executor.main()
        check("driver: alert live.json rusak tidak diulang < 1 jam",
              sum("live.json rusak" in t for t in sent) == 1, len(sent))
        del good
    finally:
        hl.HLClient, notify.send, notify.configured = real_client, real_send, real_conf
        os.chdir(cwd)
        os.environ.clear()
        os.environ.update(env)
        shutil.rmtree(work, ignore_errors=True)


def test_driver():
    import json
    import shutil
    import tempfile
    import mex.hl_client as hl
    from mex import notify
    import run_executor

    work, cwd = tempfile.mkdtemp(), os.getcwd()
    env = dict(os.environ)
    real_client, real_send, real_conf = hl.HLClient, notify.send, notify.configured
    sent = []
    notify.send = lambda text: sent.append(text) or True
    notify.configured = lambda: True
    try:
        # The driver loads config.yaml from the repo, but reads/writes state/ in cwd.
        os.chdir(work)
        os.makedirs("state")
        with open("state/position.json", "w", encoding="utf-8") as fh:
            json.dump(strat(SOLUSDT={"pending": pending("S1", expires="2099-01-01T00:00:00+00:00")}), fh)

        os.environ["MEX_EXEC_MODE"] = "off"
        check("driver: mode off keluar 0 tanpa kunci", run_executor.main() == 0)
        os.environ["MEX_EXEC_MODE"] = "live"
        os.environ.pop("HL_AGENT_KEY", None)
        check("driver: tanpa kunci -> gagal, tidak ada order", run_executor.main() == 1)

        os.environ["HL_AGENT_KEY"] = "0x" + "ab" * 32
        fake = FakeHL({"SOL": 120.0})
        fake._mids["SOL"] = 120.0
        hl.HLClient = lambda key, account: fake
        rc = run_executor.main()
        check("driver: run normal keluar 0", rc == 0, rc)
        live = json.load(open("state/live.json", encoding="utf-8"))
        check("driver: live.json tertulis", live["symbols"]["SOLUSDT"]["status"] in ("open", "skipped"),
              live["symbols"])
        check("driver: live_trades.csv tertulis", os.path.getsize("state/live_trades.csv") > 0)

        fake.agent_address = "0x" + "9" * 40
        sent.clear()
        check("driver: kunci salah -> exit 2", run_executor.main() == 2)
        check("driver: alert berhenti terkirim", len(sent) == 1 and "berhenti" in sent[0])
        run_executor.main()
        check("driver: alert yang sama tidak diulang tiap 10 menit", len(sent) == 1, len(sent))
        dumped = open("state/live.json", encoding="utf-8").read() + open("state/live_trades.csv", encoding="utf-8").read()
        check("driver: kunci tidak pernah tertulis ke state", "ab" * 32 not in dumped)
    finally:
        hl.HLClient, notify.send, notify.configured = real_client, real_send, real_conf
        os.chdir(cwd)
        os.environ.clear()
        os.environ.update(env)
        shutil.rmtree(work, ignore_errors=True)


def test_secret_shape_is_explained():
    import json
    import shutil
    import tempfile
    from mex import notify
    import run_executor

    good = "ab" * 32
    check("private key 64 hex diterima", run_executor.key_problem(good) is None)
    check("private key dengan 0x diterima", run_executor.key_problem("0x" + good) is None)
    addr = "0x5dcd653c361737ee61cb5b4863162e97796696a4"
    msg = run_executor.key_problem(addr)
    check("alamat 20 byte dikenali sebagai ALAMAT, bukan private key", msg and "ALAMAT" in msg, msg)
    check("pesan tidak pernah memuat isi secret", addr[2:] not in msg and "5dcd" not in msg)
    odd = run_executor.key_problem("abc123")
    check("isi lain: panjangnya disebut, isinya tidak", "6 karakter" in odd and "abc123" not in odd, odd)

    work, cwd, env = tempfile.mkdtemp(), os.getcwd(), dict(os.environ)
    real_send, real_conf = notify.send, notify.configured
    sent = []
    notify.send = lambda text: sent.append(text) or True
    notify.configured = lambda: True
    try:
        os.chdir(work)
        os.makedirs("state")
        with open("state/position.json", "w", encoding="utf-8") as fh:
            json.dump(strat(), fh)
        os.environ.update(MEX_EXEC_MODE="dry", HL_AGENT_KEY=addr)
        rc = run_executor.main()
        check("driver: secret berisi alamat -> exit 2, tanpa crash", rc == 2, rc)
        check("driver: alert menjelaskan cara memperbaiki", len(sent) == 1 and "ALAMAT" in sent[0], sent)
        run_executor.main()
        check("driver: alert secret salah tidak diulang tiap 10 menit", len(sent) == 1, len(sent))
    finally:
        notify.send, notify.configured = real_send, real_conf
        os.chdir(cwd)
        os.environ.clear()
        os.environ.update(env)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    print("test_executor.py")
    for t in (test_rounding, test_agent_verification, test_mode_off_and_dry, test_live_entry_long,
              test_short_and_min_order, test_entry_refusals, test_margin_counts_entries_in_same_run,
              test_manage_mode_keeps_signal_open,
              test_stop_follows_strategy_trail, test_exits, test_crash_recovery,
              test_failures_never_leave_naked_position, test_missed_and_orphans,
              test_orders_carry_bot_cloid, test_symbol_error_does_not_stop_others,
              test_dry_still_protects_live_positions, test_adopt_after_lost_state,
              test_min_order_at_limit_price, test_user_orders_untouched,
              test_circuit_breaker, test_alert_text_is_escaped,
              test_driver, test_driver_delivery_and_state, test_secret_shape_is_explained):
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    sys.exit(1 if FAIL else 0)
