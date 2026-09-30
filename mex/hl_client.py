"""Thin adapter over the official Hyperliquid SDK -- the only file that talks to it.

The executor depends on the small interface below, never on the SDK directly,
so its logic is tested against a fake exchange (tests/test_executor.py). The SDK
is imported lazily: its eth-account dependency loads a native DLL that Windows
Application Control blocks on the author's machine, so importing it at module
level would break every offline test there.

Account model: the account runs in Hyperliquid's "unified account" mode. The
docs are explicit that API clients must then read balances from the SPOT
clearinghouse state; the perp state's accountValue is 0 even with funds in.
"""
import hashlib
import math
import time

MAINNET = "https://api.hyperliquid.xyz"

# A market order is an IOC limit this far through the mid. It is the worst
# price accepted, not the expected fill.
ENTRY_SLIPPAGE = 0.01
# Limit price of a triggered stop-market: the worst fill accepted once it fires.
# Wide on purpose -- a stop that refuses to fill in a crash is worse than a bad fill.
STOP_SLIPPAGE = 0.10


# Every order the bot sends carries a client order id (cloid) that starts with
# these bytes ("MEX" + a kind byte). It is how the bot tells its own orders from
# ones placed by hand in the UI, and -- for the entry -- how it proves after a
# lost state file that a position on the account is the one it opened.
BOT_PREFIX = "0x4d4558"
KIND_ENTRY, KIND_STOP, KIND_CLOSE = "01", "02", "03"


def entry_cloid(symbol: str, signal_id: str) -> str:
    """Deterministic: the same signal always maps to the same entry cloid."""
    h = hashlib.sha256(f"{symbol}|{signal_id}".encode()).hexdigest()
    return BOT_PREFIX + KIND_ENTRY + h[:24]


def fresh_cloid(kind: str, symbol: str, signal_id: str) -> str:
    """Unique per order (stops are replaced; a cloid is never reused)."""
    h = hashlib.sha256(f"{symbol}|{signal_id}".encode()).hexdigest()
    return BOT_PREFIX + kind + h[:12] + f"{time.time_ns() // 1000 % 2**48:012x}"


def is_bot_cloid(cloid) -> bool:
    return isinstance(cloid, str) and cloid.lower().startswith(BOT_PREFIX)


def ioc_px(mid: float, is_buy: bool, sz_decimals: int) -> float:
    """Limit price of a market (IOC) order: the worst fill accepted."""
    return round_px(mid * (1 + ENTRY_SLIPPAGE if is_buy else 1 - ENTRY_SLIPPAGE), sz_decimals)


def round_px(px: float, sz_decimals: int) -> float:
    """Hyperliquid perp tick rule: <= 5 significant figures and <= 6 - szDecimals decimals."""
    return round(float(f"{px:.5g}"), 6 - sz_decimals)


def round_sz_down(sz: float, sz_decimals: int) -> float:
    q = 10 ** sz_decimals
    return math.floor(sz * q + 1e-9) / q


def round_sz_up(sz: float, sz_decimals: int) -> float:
    q = 10 ** sz_decimals
    return math.ceil(sz * q - 1e-9) / q


def order_status(resp) -> dict:
    """First status of an order/modify response, or {'error': ...}.

    Hyperliquid answers {"status": "ok", "response": {"data": {"statuses": [...]}}}
    and puts per-order failures INSIDE statuses with status still "ok".
    """
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        return {"error": f"respons ditolak: {resp}"}
    data = (resp.get("response") or {}).get("data") or {}
    st = data.get("statuses") or []
    if not st:
        return {"ok": True}
    return st[0] if isinstance(st[0], dict) else {"ok": st[0]}


class HLClient:
    """Live client. Signs with the API (agent) wallet on behalf of `account`."""

    def __init__(self, agent_key: str, account: str, base_url: str = MAINNET):
        import eth_account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info

        self.account = account
        self.agent_address = eth_account.Account.from_key(agent_key).address
        self.info = Info(base_url, skip_ws=True)
        self.exchange = Exchange(eth_account.Account.from_key(agent_key), base_url,
                                 account_address=account)
        meta = self.info.meta()
        self._sz_dec = {u["name"]: u["szDecimals"] for u in meta["universe"]}

    # -- reads ------------------------------------------------------------ #
    def agents(self) -> list:
        return self.info.extra_agents(self.account)

    def sz_decimals(self, coin: str) -> int:
        return self._sz_dec[coin]

    def mids(self) -> dict:
        return {k: float(v) for k, v in self.info.all_mids().items()}

    def usdc_balance(self) -> float:
        for b in self.info.spot_user_state(self.account).get("balances", []):
            if b["coin"] == "USDC":
                return float(b["total"])
        return 0.0

    def positions(self) -> dict:
        """{coin: {"szi": signed size, "entry_px", "isolated", "margin_used"}}"""
        out = {}
        for ap in self.info.user_state(self.account).get("assetPositions", []):
            p = ap["position"]
            szi = float(p["szi"])
            if szi == 0:
                continue
            out[p["coin"]] = {"szi": szi, "entry_px": float(p["entryPx"]),
                              "isolated": p["leverage"]["type"] == "isolated",
                              "margin_used": float(p["marginUsed"])}
        return out

    def stop_orders(self) -> dict:
        """{coin: [ {oid, trigger_px, sz, is_buy} ]} -- resting reduce-only triggers."""
        out = {}
        for o in self.info.frontend_open_orders(self.account):
            if o.get("isTrigger") and o.get("reduceOnly"):
                out.setdefault(o["coin"], []).append({
                    "oid": int(o["oid"]), "trigger_px": float(o["triggerPx"]),
                    "sz": float(o["sz"]), "is_buy": o["side"] == "B",
                    "cloid": o.get("cloid")})
        return out

    def entry_filled(self, cloid: str) -> bool:
        """True if the bot's entry order with this cloid was (at least partly) filled."""
        from hyperliquid.utils.types import Cloid
        r = self.info.query_order_by_cloid(self.account, Cloid.from_str(cloid))
        if not isinstance(r, dict) or r.get("status") != "order":
            return False
        o = r.get("order") or {}
        inner = o.get("order") or {}
        filled = float(inner.get("origSz", 0) or 0) - float(inner.get("sz", 0) or 0)
        return o.get("status") == "filled" or filled > 0

    # -- writes ----------------------------------------------------------- #
    def set_isolated(self, coin: str, leverage: int) -> dict:
        return order_status(self.exchange.update_leverage(leverage, coin, is_cross=False))

    def market(self, coin: str, is_buy: bool, sz: float, mid: float,
               reduce_only: bool = False, cloid: str | None = None) -> dict:
        from hyperliquid.utils.types import Cloid
        px = ioc_px(mid, is_buy, self.sz_decimals(coin))
        return order_status(self.exchange.order(
            coin, is_buy, sz, px, {"limit": {"tif": "Ioc"}}, reduce_only=reduce_only,
            cloid=Cloid.from_str(cloid) if cloid else None))

    def _stop_args(self, coin, is_buy, trigger_px):
        d = self.sz_decimals(coin)
        trig = round_px(trigger_px, d)
        limit = round_px(trig * (1 + STOP_SLIPPAGE if is_buy else 1 - STOP_SLIPPAGE), d)
        return limit, {"trigger": {"triggerPx": trig, "isMarket": True, "tpsl": "sl"}}

    def place_stop(self, coin: str, is_buy: bool, sz: float, trigger_px: float,
                   cloid: str | None = None) -> dict:
        from hyperliquid.utils.types import Cloid
        limit, ot = self._stop_args(coin, is_buy, trigger_px)
        return order_status(self.exchange.order(
            coin, is_buy, sz, limit, ot, reduce_only=True,
            cloid=Cloid.from_str(cloid) if cloid else None))

    def modify_stop(self, oid: int, coin: str, is_buy: bool, sz: float,
                    trigger_px: float, cloid: str | None = None) -> dict:
        from hyperliquid.utils.types import Cloid
        limit, ot = self._stop_args(coin, is_buy, trigger_px)
        return order_status(self.exchange.modify_order(
            oid, coin, is_buy, sz, limit, ot, reduce_only=True,
            cloid=Cloid.from_str(cloid) if cloid else None))

    def cancel(self, coin: str, oid: int) -> dict:
        return order_status(self.exchange.cancel(coin, oid))

    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)
