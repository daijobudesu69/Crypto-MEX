"""MEX 3.0 executor: mirrors the strategy state machine onto a Hyperliquid account.

The strategy (run_signal.py -> state/position.json) stays the single source of
truth for WHAT to hold. This module only reconciles the exchange with it:

  pending signal, nothing live  -> market entry, then a stop at 1R right away
  strategy position, live open  -> keep one reduce-only stop at the strategy's
                                   trail (the level carried into the next bar)
  strategy exited, live open    -> close at market
  live position gone            -> the exchange stop fired; record it

Why a 1R stop the moment the entry fills, when the backtest has no stop on the
entry bar: across 1,216 backtest trades on these 13 coins, 62 entry bars moved
more than 1R against the position and every one of those trades lost anyway
(mean -1.02R); the worst entry bar reached 2.15R. The early stop changes no
outcome the backtest counted as a win and caps the tail.

Hyperliquid has a native trailing stop in its UI but not in its API (checked
2026-09-29), so the trail is a stop-market that is moved once per closed 4H
bar -- which is exactly the backtest's exit model, not an approximation of it.

Modes (repo variable MEX_EXEC_MODE):
  off     do nothing at all
  dry     read the account, log what it WOULD do, send nothing to the exchange
  manage  no new entries; keep managing stops and exits of open trades
  live    everything
"""
from dataclasses import dataclass, field

import pandas as pd

from . import datafeed, execution
from .hl_client import round_px, round_sz_down, round_sz_up

MODES = ("off", "dry", "manage", "live")
SCHEMA = 1
HISTORY_KEPT = 500
REALERT = pd.Timedelta("24h")
# Leave this share of free balance unused: the margin an order takes is only
# known exactly after the fill.
MARGIN_HEADROOM = 0.95
MAX_RAISED_RISK = 2.0


class Halt(RuntimeError):
    """Refuse to touch the account at all (wrong key, expired agent, ...)."""


@dataclass
class Result:
    live: dict
    events: list = field(default_factory=list)     # Telegram-worthy
    rows: list = field(default_factory=list)       # state/live_trades.csv


def empty_state() -> dict:
    return {"schema": SCHEMA, "symbols": {}, "handled": [], "alerts": {}}


def coin_of(symbol: str) -> str:
    return datafeed.INSTRUMENTS[symbol]["hyperliquid"][0]


def verify_agent(client, ex: dict, now_ms: int) -> None:
    want = ex["agent_address"].lower()
    if client.agent_address.lower() != want:
        raise Halt(f"kunci di secret menghasilkan {client.agent_address}, bukan API "
                   f"wallet MEX.bot {ex['agent_address']} -- secret salah isi")
    for a in client.agents():
        if a["address"].lower() == want:
            if int(a["validUntil"]) <= now_ms:
                raise Halt("API wallet MEX.bot sudah kedaluwarsa di Hyperliquid")
            return
    raise Halt(f"API wallet {ex['agent_address']} tidak terdaftar di akun "
               f"{ex['account_address']}")


class Executor:
    def __init__(self, client, ex: dict, params, mode: str, now: pd.Timestamp,
                 persist=lambda live: None):
        if mode not in MODES:
            raise ValueError(f"MEX_EXEC_MODE '{mode}' tidak dikenal, pilih {MODES}")
        self.c, self.ex, self.p, self.mode, self.now = client, ex, params, mode, now
        self.persist = persist
        self.res = None

    # ------------------------------------------------------------------ #
    def run(self, strategy: dict, live: dict | None) -> Result:
        live = live or empty_state()
        self.res = Result(live=live)
        if self.mode == "off":
            return self.res
        verify_agent(self.c, self.ex, int(self.now.timestamp() * 1000))

        self.mids = self.c.mids()
        self.balance = self.c.usdc_balance()
        self.positions = self.c.positions()
        self.stops = self.c.stop_orders()

        slots = strategy.get("symbols") or {}
        for sym in datafeed.SYMBOLS:
            self._symbol(sym, slots.get(sym) or {})
        self._orphans()
        del live["handled"][:-HISTORY_KEPT]
        return self.res

    # ------------------------------------------------------------------ #
    def _symbol(self, sym, slot):
        live = self.res.live
        coin = coin_of(sym)
        pos, pend = slot.get("position"), slot.get("pending")
        strat_sid = (pos or {}).get("signal_id") or (pend or {}).get("signal_id")
        t = live["symbols"].get(sym)

        if t and t["status"] == "entering":
            self._recover_entry(sym, t)
        if t and t["status"] == "open":
            if coin not in self.positions:
                self._closed_on_exchange(sym, t)
            elif strat_sid != t["signal_id"]:
                self._close(sym, t, "strategi exit (trail tersentuh di candle)")
            else:
                self._keep_stop(sym, t, pos if (pos or {}).get("signal_id") == t["signal_id"] else None)

        t = live["symbols"].get(sym)
        if t and t["status"] in ("open", "entering"):
            return
        if not strat_sid or strat_sid in live["handled"]:
            return
        if pend and pend.get("signal_id") == strat_sid:
            self._enter(sym, pend)
        else:
            # The strategy already holds a position this executor never opened:
            # it was not running when the signal came. Chasing it late is not
            # the backtested trade, so it is recorded and left alone.
            self._handled(sym, strat_sid, "skipped",
                          "terlewat: executor tidak jalan saat sinyal masih berlaku")

    # ------------------------------------------------------------------ #
    def _enter(self, sym, pend):
        coin, sid = coin_of(sym), pend["signal_id"]
        side = int(pend["side"])
        if self.mode == "manage":
            return                       # not recorded: switching to live in time still enters
        if self.now > pd.Timestamp(pend["expires_at"]):
            return self._handled(sym, sid, "skipped", "sinyal sudah hangus")
        mid = self.mids.get(coin)
        if mid is None:
            return self._handled(sym, sid, "skipped", f"tidak ada harga {coin}")
        if not (pend["zone_low"] <= mid <= pend["zone_high"]):
            return self._handled(sym, sid, "skipped",
                                 f"harga {mid:g} di luar zona {pend['zone_low']:g}–{pend['zone_high']:g}")
        if coin in self.positions:
            return self._handled(sym, sid, "skipped",
                                 f"sudah ada posisi {coin} di akun (bukan dari bot)")

        sz_plan = execution.size(sym, mid, pend["r_est"], side, self.balance,
                                 self.p.risk_pct, self.ex["leverage"])
        # Raising to the $10 minimum was accepted at ~1.35x the target risk with
        # a ~$100 balance. On a small balance the same rule multiplies it
        # (balance $3 -> 12% on one trade), so it is capped.
        if sz_plan.raised_to_min and sz_plan.risk_usd > MAX_RAISED_RISK * sz_plan.risk_target_usd:
            return self._handled(sym, sid, "skipped",
                                 f"order minimum $10 berarti risiko ${sz_plan.risk_usd:.2f} "
                                 f"(> {MAX_RAISED_RISK:g}x target ${sz_plan.risk_target_usd:.2f}); "
                                 f"saldo terlalu kecil untuk jarak stop ini")
        used = sum(p["margin_used"] for p in self.positions.values() if p["isolated"])
        free = self.balance - used
        if sz_plan.margin_usd > free * MARGIN_HEADROOM:
            return self._handled(sym, sid, "skipped",
                                 f"margin kurang: butuh ${sz_plan.margin_usd:.2f}, "
                                 f"tersedia ${free:.2f}")
        dec = self.c.sz_decimals(coin)
        sz = round_sz_down(sz_plan.order_usd / mid, dec)
        if sz * mid < execution.HL_MIN_ORDER_USD:
            sz = round_sz_up(execution.HL_MIN_ORDER_USD / mid, dec)
        stop_est = mid - side * pend["r_est"]
        plan = (f"{'LONG' if side > 0 else 'SHORT'} {sz:g} {coin} ≈ ${sz * mid:.2f} "
                f"@ ~{mid:g}, stop {round_px(stop_est, dec):g}, risiko ≈ "
                f"${sz * pend['r_est']:.2f} ({sz * pend['r_est'] / self.balance * 100:.2f}% "
                f"dari ${self.balance:.2f})")

        if self.mode == "dry":
            return self._handled(sym, sid, "dry", "DRY-RUN, tidak dikirim: " + plan)

        t = {"signal_id": sid, "status": "entering", "side": side, "coin": coin,
             "size": sz, "entry_px": None, "stop_px": stop_est, "stop_oid": None,
             "r_est": pend["r_est"], "balance_at_entry": self.balance,
             "opened_at": self.now.isoformat(), "closed_at": None, "reason": ""}
        self.res.live["symbols"][sym] = t
        # Written BEFORE the order goes out: a run that dies between the fill and
        # the next save finds "entering" and recovers the position from the
        # exchange instead of leaving it without a stop.
        self.persist(self.res.live)

        lev = self.c.set_isolated(coin, self.ex["leverage"])
        if "error" in lev:
            return self._abort_entry(sym, t, f"gagal set isolated {self.ex['leverage']}x: {lev['error']}")
        fill = self.c.market(coin, side > 0, sz, mid).get("filled")
        if not fill or float(fill.get("totalSz", 0)) <= 0:
            return self._abort_entry(sym, t, "order market tidak terisi")
        t.update(size=float(fill["totalSz"]), entry_px=float(fill["avgPx"]),
                 stop_px=float(fill["avgPx"]) - side * pend["r_est"])
        # The snapshot taken at the start of the run must see this position, or
        # a second signal in the same run would count margin that is now used.
        self.positions[coin] = {"szi": side * t["size"], "entry_px": t["entry_px"],
                                "isolated": True,
                                "margin_used": t["size"] * t["entry_px"] / self.ex["leverage"]}
        st = self.c.place_stop(coin, side < 0, t["size"], t["stop_px"])
        if "resting" not in st:
            # Never leave a real position without a stop: close it at once.
            closed = self.c.market(coin, side < 0, t["size"], self.mids[coin],
                                   reduce_only=True).get("filled")
            self._handled(sym, sid, None, None)
            if closed:
                self.positions.pop(coin, None)
                t.update(status="closed", closed_at=self.now.isoformat(),
                         exit_px=float(closed["avgPx"]),
                         reason=f"stop gagal dipasang ({st}), posisi langsung ditutup")
                self._event(sym, "error", f"🚨 {sym}: stop gagal dipasang, posisi langsung ditutup. {st}")
                self.persist(self.res.live)
                return self._row(sym, t, "ENTRY_ABORTED")
            # Could not close either. Keep it tracked as open with no stop: the
            # next run (10 minutes) finds no stop and places one.
            t.update(status="open", stop_oid=None, reason="stop & penutupan gagal")
            self.persist(self.res.live)
            self._event(sym, "error", f"🚨🚨 {sym}: posisi TERBUKA TANPA STOP — stop dan "
                                      f"penutupan darurat gagal. Dicoba lagi tiap run. Cek manual! {st}")
            return self._row(sym, t, "ENTRY_NO_STOP")
        t.update(status="open", stop_oid=int(st["resting"]["oid"]))
        self._handled(sym, sid, None, None)
        self.persist(self.res.live)
        risk = t["size"] * pend["r_est"]
        self._event(sym, "entry",
                    f"🟢 <b>LIVE ENTRY {sym}</b> {'LONG' if side > 0 else 'SHORT'}\n"
                    f"{t['size']:g} {coin} @ {t['entry_px']:g} (≈ ${t['size'] * t['entry_px']:.2f})\n"
                    f"stop {round_px(t['stop_px'], dec):g} · risiko ≈ ${risk:.2f} "
                    f"({risk / self.balance * 100:.2f}%) · isolated {self.ex['leverage']}x\n"
                    f"id: {sid}")
        self._row(sym, t, "ENTRY")

    def _abort_entry(self, sym, t, why):
        t.update(status="skipped", closed_at=self.now.isoformat(), reason=why)
        self._handled(sym, t["signal_id"], None, None)
        self._event(sym, "error", f"⚠️ {sym}: entry dibatalkan — {why}")
        self._row(sym, t, "ENTRY_FAILED")
        self.persist(self.res.live)

    def _recover_entry(self, sym, t):
        p = self.positions.get(t["coin"])
        if p and (p["szi"] > 0) == (t["side"] > 0):
            t.update(status="open", size=abs(p["szi"]), entry_px=p["entry_px"],
                     stop_px=p["entry_px"] - t["side"] * t["r_est"],
                     reason="dipulihkan setelah run terputus")
            self._event(sym, "error", f"⚠️ {sym}: run sebelumnya terputus setelah entry; "
                                      f"posisi dipulihkan, stop dipasang ulang")
        else:
            t.update(status="skipped", closed_at=self.now.isoformat(),
                     reason="run terputus sebelum order terisi")
        self._handled(sym, t["signal_id"], None, None)

    # ------------------------------------------------------------------ #
    def _keep_stop(self, sym, t, strat_pos):
        coin = t["coin"]
        dec = self.c.sz_decimals(coin)
        size = abs(self.positions[coin]["szi"])
        # Before the entry bar closes the strategy has no position yet, so the
        # 1R stop set at entry stands. From the first close on, the stop is the
        # strategy's trail -- exactly what the backtest tests the next bar against.
        want = strat_pos["trail"] if strat_pos else t["stop_px"]
        want_px = round_px(want, dec)
        mine = self.stops.get(coin, [])
        keep = next((o for o in mine if o["oid"] == t.get("stop_oid")), mine[0] if mine else None)
        live = self.mode in ("live", "manage")
        for extra in [o for o in mine if o is not keep]:
            if live:
                self.c.cancel(coin, extra["oid"])
        if keep is None:
            if not live:
                return
            st = self.c.place_stop(coin, t["side"] < 0, size, want)
            if "resting" in st:
                t["stop_oid"] = int(st["resting"]["oid"])
                self._event(sym, "error", f"⚠️ {sym}: stop tidak ada di bursa, dipasang ulang di {want_px:g}")
            else:
                self._alert(f"stopfail:{sym}", f"🚨 {sym}: posisi TANPA stop, gagal memasang: {st}")
        elif abs(keep["trigger_px"] - want_px) > 0 or abs(keep["sz"] - size) > 1e-12:
            if not live:
                return
            st = self.c.modify_stop(keep["oid"], coin, t["side"] < 0, size, want)
            if "error" in st:
                self._alert(f"modfail:{sym}", f"⚠️ {sym}: gagal menggeser stop ke {want_px:g}: {st['error']}")
                return
            t["stop_oid"] = int((st.get("resting") or {}).get("oid", keep["oid"]))
        t["stop_px"] = want

    def _close(self, sym, t, why):
        coin = t["coin"]
        if self.mode not in ("live", "manage"):
            return
        size = abs(self.positions[coin]["szi"])
        r = self.c.market(coin, t["side"] < 0, size, self.mids[coin], reduce_only=True)
        fill = r.get("filled")
        if not fill:
            self._alert(f"closefail:{sym}", f"🚨 {sym}: gagal menutup posisi ({why}): {r}")
            return
        for o in self.stops.pop(coin, []):
            self.c.cancel(coin, o["oid"])
        self.positions.pop(coin, None)
        self._finish(sym, t, float(fill["avgPx"]), why)

    def _closed_on_exchange(self, sym, t):
        for o in self.stops.pop(t["coin"], []):
            if self.mode in ("live", "manage"):
                self.c.cancel(t["coin"], o["oid"])
        self._finish(sym, t, t["stop_px"], "stop tereksekusi di bursa (harga exit perkiraan)")

    def _finish(self, sym, t, exit_px, why):
        side = t["side"]
        pnl = (exit_px - t["entry_px"]) * side * t["size"]
        r_mult = (exit_px - t["entry_px"]) * side / t["r_est"]
        t.update(status="closed", closed_at=self.now.isoformat(), reason=why, exit_px=exit_px)
        self._event(sym, "exit",
                    f"{'✅' if pnl >= 0 else '🛑'} <b>LIVE EXIT {sym}</b>\n"
                    f"{t['entry_px']:g} → ~{exit_px:g} · {r_mult:+.2f} R ≈ ${pnl:+.2f} (sebelum fee)\n"
                    f"{why}")
        self._row(sym, t, "EXIT", pnl=pnl, r_mult=r_mult)
        self.persist(self.res.live)

    # ------------------------------------------------------------------ #
    def _orphans(self):
        tracked = {t["coin"] for t in self.res.live["symbols"].values() if t["status"] == "open"}
        ours = {coin_of(s) for s in datafeed.SYMBOLS}
        for coin in sorted((set(self.positions) & ours) - tracked):
            self._alert(f"orphan:{coin}",
                        f"⚠️ Ada posisi {coin} di akun yang tidak dibuka bot. Bot tidak "
                        f"menyentuhnya dan tidak akan entry {coin} selama posisi itu ada.")

    def _handled(self, sym, sid, status, reason):
        if sid not in self.res.live["handled"]:
            self.res.live["handled"].append(sid)
        if status:
            print(f"[exec] {sym} {sid}: {status} — {reason}")
            self._row(sym, {"signal_id": sid, "status": status, "reason": reason}, status.upper())
            if status == "dry":
                self._event(sym, "dry", f"🧪 {sym}: {reason}")

    def _alert(self, key, text):
        last = self.res.live["alerts"].get(key)
        if last and self.now - pd.Timestamp(last) < REALERT:
            return
        self.res.live["alerts"][key] = self.now.isoformat()
        self._event(key, "error", text)

    def _event(self, sym, kind, text):
        self.res.events.append({"symbol": sym, "kind": kind, "text": text})

    def _row(self, sym, t, action, pnl=None, r_mult=None):
        self.res.rows.append({
            "logged_at_utc": self.now.isoformat(), "mode": self.mode, "symbol": sym,
            "action": action, "signal_id": t.get("signal_id"), "side": t.get("side"),
            "size": t.get("size"), "entry_px": t.get("entry_px"),
            "exit_px": t.get("exit_px"), "stop_px": t.get("stop_px"),
            "balance": self.balance, "pnl_usd": pnl, "result_R": r_mult,
            "reason": t.get("reason")})
