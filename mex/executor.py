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
  dry     no new entries: log what it WOULD enter. Positions the bot already
          holds are still protected (stop trailed, strategy exits closed) --
          switching live -> dry must never leave a real position unmanaged.
  manage  no new entries; keep managing stops and exits of open trades
  live    everything

Circuit breaker (execution.max_drawdown_pct): once the USDC balance falls that
far below its highest recorded value, new entries stop until the user resets
it (repo variable MEX_BREAKER_RESET). Open trades keep being managed.
"""
import traceback
from dataclasses import dataclass, field

import pandas as pd

from . import datafeed, execution
from .hl_client import (KIND_CLOSE, KIND_STOP, entry_cloid, fresh_cloid, ioc_px,
                        is_bot_cloid, round_px, round_sz_down, round_sz_up)
from .notify import esc

MODES = ("off", "dry", "manage", "live")
SCHEMA = 1
HISTORY_KEPT = 500
REALERT = pd.Timedelta("24h")
# A position without a working stop is the one failure that must not wait a day.
REALERT_URGENT = pd.Timedelta("1h")
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
    errors: list = field(default_factory=list)     # symbols whose handling raised


def empty_state() -> dict:
    return {"schema": SCHEMA, "symbols": {}, "handled": [], "alerts": {},
            "peak_balance": None, "breaker": None, "breaker_reset_seen": ""}


def reset_token(now: pd.Timestamp) -> str:
    """A ready-to-paste MEX_BREAKER_RESET value; any value not used before works.

    The alert used to say `--body $(date +%s)`, which only runs in bash. The
    owner's terminal is PowerShell, where that line fails at the exact moment
    the breaker has tripped.
    """
    return f"reset-{now:%Y%m%d-%H%M}"


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
                 persist=lambda live: None, breaker_reset: str = ""):
        if mode not in MODES:
            raise ValueError(f"MEX_EXEC_MODE '{mode}' tidak dikenal, pilih {MODES}")
        self.c, self.ex, self.p, self.mode, self.now = client, ex, params, mode, now
        self.persist = persist
        self.breaker_reset = (breaker_reset or "").strip()
        self.res = None

    # ------------------------------------------------------------------ #
    def run(self, strategy: dict, live: dict | None) -> Result:
        live = live or empty_state()
        for k, v in empty_state().items():
            live.setdefault(k, v)
        self.res = Result(live=live)
        if self.mode == "off":
            return self.res
        verify_agent(self.c, self.ex, int(self.now.timestamp() * 1000))

        self.mids = self.c.mids()
        self.balance = self.c.usdc_balance()
        self.positions = self.c.positions()
        self.stops = self.c.stop_orders()

        self._breaker()
        held = [s for s, t in live["symbols"].items() if t["status"] in ("open", "entering")]
        if self.mode == "dry" and held:
            self._alert("dry-with-positions",
                        f"⚠️ Mode dry, tapi ada posisi live ({', '.join(held)}). Bot tetap "
                        f"menggeser stop dan menutup posisi itu saat strategi exit; hanya "
                        f"entry baru yang tidak dikirim.")

        slots = strategy.get("symbols") or {}
        for sym in datafeed.SYMBOLS:
            # One symbol failing -- a renamed coin, a missing price, a timeout --
            # must not stop the stops of the other twelve from being managed.
            try:
                self._symbol(sym, slots.get(sym) or {})
            except Halt:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[exec] {sym} ERROR: {type(e).__name__}: {e}")
                print(traceback.format_exc())
                self.res.errors.append(sym)
                t = live["symbols"].get(sym) or {}
                held_here = t.get("status") in ("open", "entering")
                self._alert(f"symerr:{sym}",
                            f"{'🚨' if held_here else '⚠️'} {sym}: executor error "
                            f"{type(e).__name__}: {esc(str(e)[:300])}"
                            + (" — posisi live di simbol ini mungkin tidak terjaga, cek manual. "
                               if held_here else ". ")
                            + "Simbol lain tetap dikelola; dicoba lagi tiap run.",
                            every=REALERT_URGENT if held_here else REALERT)
                self.persist(live)
        try:
            self._orphans()
        except Exception as e:  # noqa: BLE001
            print(f"[exec] cek posisi manual gagal: {type(e).__name__}: {e}")
        del live["handled"][:-HISTORY_KEPT]
        return self.res

    # ------------------------------------------------------------------ #
    def _breaker(self):
        """Stop new entries once the balance falls max_drawdown_pct below its peak."""
        limit = self.ex.get("max_drawdown_pct")
        live = self.res.live
        if not limit or self.balance <= 0:
            return
        if self.breaker_reset and self.breaker_reset != live.get("breaker_reset_seen"):
            # Also the way to re-base the peak after a withdrawal, which would
            # otherwise read as a drawdown.
            live["breaker_reset_seen"] = self.breaker_reset
            live["peak_balance"] = self.balance
            if live.get("breaker"):
                self._event("breaker", "breaker",
                            f"✅ Circuit breaker di-reset. Puncak saldo baru ${self.balance:.2f}; "
                            f"entry baru jalan lagi.")
            live["breaker"] = None
        peak = max(float(live.get("peak_balance") or 0.0), self.balance)
        live["peak_balance"] = peak
        dd = (1.0 - self.balance / peak) * 100.0
        if not live.get("breaker") and dd >= limit:
            live["breaker"] = {"tripped_at": self.now.isoformat(), "peak": peak,
                               "balance": self.balance, "drawdown_pct": round(dd, 2)}
            live["alerts"].pop("breaker", None)
            self.persist(live)
        if live.get("breaker"):
            b = live["breaker"]
            self._alert("breaker",
                        f"🛑 <b>Circuit breaker AKTIF</b>: saldo ${self.balance:.2f} turun "
                        f"{dd:.1f}% dari puncak ${peak:.2f} (batas {limit:g}%). Tidak ada entry "
                        f"baru; posisi yang terbuka tetap dijaga sampai selesai. Aktif sejak "
                        f"{b['tripped_at'][:16]} UTC.\nLanjutkan: <code>gh variable set "
                        f"MEX_BREAKER_RESET --body {reset_token(self.now)} --repo "
                        f"daijobudesu69/Crypto-MEX</code>")

    # ------------------------------------------------------------------ #
    def _symbol(self, sym, slot):
        live = self.res.live
        coin = coin_of(sym)
        pos, pend = slot.get("position"), slot.get("pending")
        strat_sid = (pos or {}).get("signal_id") or (pend or {}).get("signal_id")
        t = live["symbols"].get(sym)

        if t and t["status"] == "entering":
            self._recover_entry(sym, t)
        if (strat_sid and coin in self.positions and strat_sid not in live["handled"]
                and not (t and t["signal_id"] == strat_sid)):
            # live.json lost or stale (a job killed before its state was pushed):
            # the account may hold the position of the strategy's current signal.
            # The entry order's cloid proves whether the bot opened it.
            if self._adopt(sym, coin, strat_sid, pos, pend, t):
                t = live["symbols"][sym]
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
        if self.res.live.get("breaker"):
            return self._handled(sym, sid, "dry" if self.mode == "dry" else "skipped",
                                 "tidak entry: circuit breaker aktif")
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
        # The $10 minimum is met at the order's own limit price too. A short's
        # IOC limit sits 1% below the mid, so sizing to exactly $10 at the mid
        # could leave a $9.90 order that Hyperliquid rejects.
        px_floor = min(mid, ioc_px(mid, side > 0, dec))
        if sz * px_floor < execution.HL_MIN_ORDER_USD:
            sz = round_sz_up(execution.HL_MIN_ORDER_USD / px_floor * (1 + 1e-6), dec)
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
             "opened_at": self.now.isoformat(), "closed_at": None, "reason": "",
             "entry_cloid": entry_cloid(sym, sid)}
        self.res.live["symbols"][sym] = t
        # Written BEFORE the order goes out: a run that dies between the fill and
        # the next save finds "entering" and recovers the position from the
        # exchange instead of leaving it without a stop.
        self.persist(self.res.live)

        lev = self.c.set_isolated(coin, self.ex["leverage"])
        if "error" in lev:
            return self._abort_entry(sym, t, f"gagal set isolated {self.ex['leverage']}x: {lev['error']}")
        fill = self.c.market(coin, side > 0, sz, mid, cloid=t["entry_cloid"]).get("filled")
        if not fill or float(fill.get("totalSz", 0)) <= 0:
            return self._abort_entry(sym, t, "order market tidak terisi")
        t.update(size=float(fill["totalSz"]), entry_px=float(fill["avgPx"]),
                 stop_px=float(fill["avgPx"]) - side * pend["r_est"])
        # The snapshot taken at the start of the run must see this position, or
        # a second signal in the same run would count margin that is now used.
        self.positions[coin] = {"szi": side * t["size"], "entry_px": t["entry_px"],
                                "isolated": True,
                                "margin_used": t["size"] * t["entry_px"] / self.ex["leverage"]}
        st = self.c.place_stop(coin, side < 0, t["size"], t["stop_px"],
                               cloid=fresh_cloid(KIND_STOP, sym, sid))
        if "resting" not in st:
            # Never leave a real position without a stop: close it at once.
            closed = self.c.market(coin, side < 0, t["size"], self.mids[coin], reduce_only=True,
                                   cloid=fresh_cloid(KIND_CLOSE, sym, sid)).get("filled")
            self._handled(sym, sid, None, None)
            if closed:
                self.positions.pop(coin, None)
                t.update(status="closed", closed_at=self.now.isoformat(),
                         exit_px=float(closed["avgPx"]),
                         reason=f"stop gagal dipasang ({st}), posisi langsung ditutup")
                self._event(sym, "error", f"🚨 {sym}: stop gagal dipasang, posisi langsung "
                                          f"ditutup. {esc(st)}")
                self.persist(self.res.live)
                return self._row(sym, t, "ENTRY_ABORTED")
            # Could not close either. Keep it tracked as open with no stop: the
            # next run (10 minutes) finds no stop and places one.
            t.update(status="open", stop_oid=None, reason="stop & penutupan gagal")
            self.persist(self.res.live)
            self._event(sym, "error", f"🚨🚨 {sym}: posisi TERBUKA TANPA STOP — stop dan "
                                      f"penutupan darurat gagal. Dicoba lagi tiap run. "
                                      f"Cek manual! {esc(st)}")
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
        self._event(sym, "error", f"⚠️ {sym}: entry dibatalkan — {esc(why)}")
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

    def _adopt(self, sym, coin, sid, pos, pend, stale):
        """Take back a position the bot opened for `sid` but has no record of."""
        src = pos or pend
        side = int(src["side"])
        p = self.positions[coin]
        if (p["szi"] > 0) != (side > 0):
            return False
        cloid = entry_cloid(sym, sid)
        if not self.c.entry_filled(cloid):
            return False
        if stale and stale["status"] == "open":
            # The record is of an earlier trade that has since ended; its exit
            # happened while the state was lost, so its price is unknown.
            self._finish(sym, stale, stale["stop_px"],
                         "selesai saat state hilang (harga exit tidak diketahui)")
        r = (pend or {}).get("r_est") or (pos or {}).get("r_usdt") \
            or abs(p["entry_px"] - float((pos or {}).get("trail") or p["entry_px"])) or None
        stop = float(pos["trail"]) if pos and pos.get("trail") is not None \
            else p["entry_px"] - side * (r or 0)
        t = {"signal_id": sid, "status": "open", "side": side, "coin": coin,
             "size": abs(p["szi"]), "entry_px": p["entry_px"], "stop_px": stop,
             "stop_oid": None, "r_est": r, "balance_at_entry": self.balance,
             "opened_at": self.now.isoformat(), "closed_at": None,
             "reason": "diadopsi ulang dari bursa (state live hilang)", "entry_cloid": cloid}
        self.res.live["symbols"][sym] = t
        self._handled(sym, sid, None, None)
        self._event(sym, "error", f"⚠️ {sym}: catatan posisi live hilang dari state; posisi "
                                  f"{sid} di bursa terbukti dibuka bot dan diambil alih lagi "
                                  f"(stop & trail dijaga seperti biasa).")
        self._row(sym, t, "ADOPTED")
        self.persist(self.res.live)
        return True

    def _bot_stops(self, coin, t):
        """Resting reduce-only triggers that belong to the bot, never the user's own."""
        return [o for o in self.stops.get(coin, [])
                if o["oid"] == t.get("stop_oid") or is_bot_cloid(o.get("cloid"))]

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
        mine = self._bot_stops(coin, t)
        closing_side = t["side"] < 0            # a long's stop sells
        keep = next((o for o in mine if o["oid"] == t.get("stop_oid")),
                    next((o for o in mine if o["is_buy"] == closing_side), None))
        for extra in [o for o in mine if o is not keep]:
            self.c.cancel(coin, extra["oid"])
        if keep is None:
            st = self.c.place_stop(coin, closing_side, size, want,
                                   cloid=fresh_cloid(KIND_STOP, sym, t["signal_id"]))
            if "resting" in st:
                t["stop_oid"] = int(st["resting"]["oid"])
                self._event(sym, "error", f"⚠️ {sym}: stop tidak ada di bursa, dipasang ulang di {want_px:g}")
            else:
                self._alert(f"stopfail:{sym}", f"🚨 {sym}: posisi TANPA stop, gagal memasang: {esc(st)}",
                            every=REALERT_URGENT)
        elif abs(keep["trigger_px"] - want_px) > 0 or abs(keep["sz"] - size) > 1e-12:
            st = self.c.modify_stop(keep["oid"], coin, closing_side, size, want,
                                    cloid=fresh_cloid(KIND_STOP, sym, t["signal_id"]))
            if "error" in st:
                self._alert(f"modfail:{sym}", f"⚠️ {sym}: gagal menggeser stop ke {want_px:g}: "
                                              f"{esc(st['error'])}", every=REALERT_URGENT)
                return
            t["stop_oid"] = int((st.get("resting") or {}).get("oid", keep["oid"]))
        t["stop_px"] = want

    def _close(self, sym, t, why):
        coin = t["coin"]
        size = abs(self.positions[coin]["szi"])
        r = self.c.market(coin, t["side"] < 0, size, self.mids[coin], reduce_only=True,
                          cloid=fresh_cloid(KIND_CLOSE, sym, t["signal_id"]))
        fill = r.get("filled")
        if not fill:
            self._alert(f"closefail:{sym}", f"🚨 {sym}: gagal menutup posisi ({why}): {esc(r)}",
                        every=REALERT_URGENT)
            return
        for o in self._bot_stops(coin, t):
            self.c.cancel(coin, o["oid"])
        self.stops.pop(coin, None)
        self.positions.pop(coin, None)
        self._finish(sym, t, float(fill["avgPx"]), why)

    def _closed_on_exchange(self, sym, t):
        for o in self._bot_stops(t["coin"], t):
            self.c.cancel(t["coin"], o["oid"])
        self.stops.pop(t["coin"], None)
        self._finish(sym, t, t["stop_px"], "stop tereksekusi di bursa (harga exit perkiraan)")

    def _finish(self, sym, t, exit_px, why):
        side = t["side"]
        pnl = (exit_px - t["entry_px"]) * side * t["size"]
        r_mult = (exit_px - t["entry_px"]) * side / t["r_est"] if t.get("r_est") else 0.0
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

    def _alert(self, key, text, every=REALERT):
        last = self.res.live["alerts"].get(key)
        if last and self.now - pd.Timestamp(last) < every:
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
