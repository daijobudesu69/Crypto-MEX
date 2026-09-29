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
from mex.hl_client import order_status, round_px, round_sz_down, round_sz_up  # noqa: E402
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

    def agents(self):
        return self._agents

    def sz_decimals(self, coin):
        return SZ_DEC[coin]

    def mids(self):
        return dict(self._mids)

    def usdc_balance(self):
        return self.balance

    def positions(self):
        return copy.deepcopy(self.pos)

    def stop_orders(self):
        out = {}
        for oid, o in self.orders.items():
            out.setdefault(o["coin"], []).append({"oid": oid, **{k: o[k] for k in ("trigger_px", "sz", "is_buy")}})
        return out

    def set_isolated(self, coin, lev):
        self.calls.append(("set_isolated", coin, lev))
        return {"error": "nope"} if "set_isolated" in self.fail else {"ok": True}

    def market(self, coin, is_buy, sz, mid, reduce_only=False):
        self.calls.append(("market", coin, is_buy, sz, reduce_only))
        name = "close" if reduce_only else "market"
        if name in self.fail:
            return {"error": "IOC tidak terisi"}
        px = self._mids[coin]
        if reduce_only:
            self.pos.pop(coin, None)
        else:
            self.pos[coin] = {"szi": sz if is_buy else -sz, "entry_px": px,
                              "isolated": True, "margin_used": sz * px / 4}
        return {"filled": {"totalSz": str(sz), "avgPx": str(px), "oid": 1}}

    def place_stop(self, coin, is_buy, sz, trigger_px):
        self.calls.append(("place_stop", coin, is_buy, sz, round_px(trigger_px, SZ_DEC[coin])))
        if "place_stop" in self.fail:
            return {"error": "Order has invalid price"}
        oid = self.next_oid
        self.next_oid += 1
        self.orders[oid] = {"coin": coin, "trigger_px": round_px(trigger_px, SZ_DEC[coin]),
                            "sz": sz, "is_buy": is_buy}
        return {"resting": {"oid": oid}}

    def modify_stop(self, oid, coin, is_buy, sz, trigger_px):
        self.calls.append(("modify_stop", coin, round_px(trigger_px, SZ_DEC[coin])))
        self.orders[oid].update(trigger_px=round_px(trigger_px, SZ_DEC[coin]), sz=sz)
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
    f.place_stop("SOL", False, 0.29, 110.0)
    f.calls.clear()
    res, _ = run(f, strat(SOLUSDT={"position": pos}), live=res.live)
    check("stop ganda: yang ekstra dibatalkan", [c[0] for c in f.calls] == ["cancel"], f.calls)


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
    check("exit lalu sinyal baru di run yang sama -> tutup lalu entry baru",
          names(f)[:2] == ["market", "cancel"] and names(f)[2:] == ["set_isolated", "market", "place_stop"],
          names(f))


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


if __name__ == "__main__":
    print("test_executor.py")
    for t in (test_rounding, test_agent_verification, test_mode_off_and_dry, test_live_entry_long,
              test_short_and_min_order, test_entry_refusals, test_margin_counts_entries_in_same_run,
              test_manage_mode_keeps_signal_open,
              test_stop_follows_strategy_trail, test_exits, test_crash_recovery,
              test_failures_never_leave_naked_position, test_missed_and_orphans, test_driver):
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    sys.exit(1 if FAIL else 0)
