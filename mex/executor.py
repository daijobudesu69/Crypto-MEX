"""Binance Futures TESTNET order execution -- entry (MARKET) + exit (TRAILING_STOP_MARKET).

TESTNET ONLY. This talks to https://testnet.binancefuture.com, which trades
play money on Binance's own paper environment. It is wired in deliberately
after the fact: README's own warning stands -- MEX failed 2 of 7 backtest
eligibility criteria (T1 out-of-sample, T9 overfitting) -- so nothing here
ever points at the real futures API (fapi.binance.com), and there is no
config path that can flip it there. Promoting this to live trading is a
separate, explicit decision for later, not a flag flip.

Two secrets gate everything:
  BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET  -- from
      https://testnet.binancefuture.com (separate account from real Binance).
Without both, configured() is False and the caller falls back to
log-only, the same pattern notify.py uses for a missing Telegram token: the
pipeline stays exercisable end-to-end before the keys exist, and a run is
never failed by execution being unset.

Mode B, same as the manual instructions in strategy.py's docstring: one
MARKET order opens the position, one TRAILING_STOP_MARKET (reduceOnly)
order rests at the exchange with the callback rate frozen at entry
(pos.callback_pct). Binance's engine then runs the trail itself, continuously,
rather than once per closed 4H bar -- so the exchange fill and step()'s own
notion of the exit can legitimately land on a different bar/price. That is a
tracking-error source of exactly the kind datafeed.py documents for the data
side, and it is why every response this module returns is logged.

If the stop order fails to place after the entry order already filled, the
position would sit unprotected -- so place_entry_with_stop() immediately
flattens it with a reduceOnly MARKET order and raises, rather than leaving a
naked position and calling it done.
"""
from . import compat  # noqa: F401
import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

import requests

BASE_URL = "https://testnet.binancefuture.com"
RECV_WINDOW = 10_000
TIMEOUT = 20

# Binance rejects a callbackRate outside [0.1, 5.0], in 0.1 steps. The
# strategy's own callback range measured in the backtest is 1.09%-5.24%
# (README), so the high end also needs clamping, not just the low end.
MIN_CALLBACK_PCT = 0.1
MAX_CALLBACK_PCT = 5.0

_filters_cache: dict[str, dict] = {}


class OrderError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
                and os.environ.get("BINANCE_TESTNET_API_SECRET", "").strip())


def _keys() -> tuple[str, str]:
    return (os.environ["BINANCE_TESTNET_API_KEY"].strip(),
            os.environ["BINANCE_TESTNET_API_SECRET"].strip())


def _signed_request(method: str, path: str, params: dict) -> dict:
    key, secret = _keys()
    params = {**params, "timestamp": int(time.time() * 1000), "recvWindow": RECV_WINDOW}
    query = urlencode(params, doseq=True)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}{path}?{query}&signature={sig}"
    r = requests.request(method, url, headers={"X-MBX-APIKEY": key}, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise OrderError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def _public_request(path: str, params: dict | None = None) -> dict:
    r = requests.get(f"{BASE_URL}{path}", params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def account_equity() -> float:
    """USDT wallet balance available for sizing (totalWalletBalance)."""
    acct = _signed_request("GET", "/fapi/v2/account", {})
    for a in acct.get("assets", []):
        if a.get("asset") == "USDT":
            return float(a["availableBalance"])
    raise OrderError("USDT tidak ditemukan di akun futures testnet")


def symbol_filters(symbol: str) -> dict:
    """LOT_SIZE stepSize, PRICE_FILTER tickSize and MIN_NOTIONAL, cached per run."""
    if symbol in _filters_cache:
        return _filters_cache[symbol]
    info = _public_request("/fapi/v1/exchangeInfo")
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            f = {flt["filterType"]: flt for flt in s["filters"]}
            out = {
                "step_size": float(f["LOT_SIZE"]["stepSize"]),
                "min_qty": float(f["LOT_SIZE"]["minQty"]),
                "tick_size": float(f["PRICE_FILTER"]["tickSize"]),
                "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5.0)),
            }
            _filters_cache[symbol] = out
            return out
    raise OrderError(f"{symbol}: tidak ditemukan di exchangeInfo testnet")


def _round_step(qty: float, step: float) -> float:
    """Round DOWN to the exchange's step size. Binance rejects a qty that isn't."""
    if step <= 0:
        return qty
    steps = int(qty / step + 1e-9)
    return round(steps * step, 10)


def size_position(symbol: str, entry_price: float, r_usdt: float, equity: float,
                   risk_pct: float) -> float:
    """qty = (risk% x equity) / 1R, per README, rounded to the symbol's step size."""
    if r_usdt <= 0:
        raise OrderError("r_usdt <= 0, tidak bisa menghitung ukuran posisi")
    raw_qty = (risk_pct / 100.0 * equity) / r_usdt
    filt = symbol_filters(symbol)
    qty = _round_step(raw_qty, filt["step_size"])
    if qty < filt["min_qty"] or qty * entry_price < filt["min_notional"]:
        raise OrderError(
            f"{symbol}: ukuran posisi {qty} di bawah minimum exchange "
            f"(min_qty={filt['min_qty']}, min_notional={filt['min_notional']})")
    return qty


def set_leverage(symbol: str, leverage: int) -> None:
    _signed_request("POST", "/fapi/v1/leverage",
                     {"symbol": symbol, "leverage": leverage})


def _market_order(symbol: str, side: str, quantity: float, reduce_only: bool) -> dict:
    return _signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": side, "type": "MARKET",
        "quantity": quantity, "reduceOnly": "true" if reduce_only else "false",
    })


def _trailing_stop_order(symbol: str, side: str, quantity: float,
                          callback_pct: float) -> dict:
    rate = round(min(max(callback_pct, MIN_CALLBACK_PCT), MAX_CALLBACK_PCT), 1)
    return _signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": side, "type": "TRAILING_STOP_MARKET",
        "quantity": quantity, "callbackRate": rate, "reduceOnly": "true",
    })


def cancel_open_orders(symbol: str) -> None:
    """Best-effort cleanup. Never raises -- a stop that already filled has
    nothing left to cancel, and that is the expected, not exceptional, case."""
    try:
        _signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    except OrderError as e:
        print(f"[executor] {symbol}: cancel_open_orders diabaikan: {e}")


def place_entry_with_stop(symbol: str, side: int, quantity: float,
                           callback_pct: float) -> dict:
    """Open the position at MARKET, then rest one TRAILING_STOP_MARKET exit.

    If the stop leg fails after the entry filled, the position is flattened
    immediately with a reduceOnly MARKET order -- an unprotected leveraged
    position left open because of an API hiccup is worse than aborting the
    trade entirely.
    """
    entry_side = "BUY" if side > 0 else "SELL"
    exit_side = "SELL" if side > 0 else "BUY"

    entry = _market_order(symbol, entry_side, quantity, reduce_only=False)
    try:
        stop = _trailing_stop_order(symbol, exit_side, quantity, callback_pct)
    except OrderError as e:
        print(f"[executor] {symbol}: trailing stop GAGAL setelah entry terisi -- "
              f"menutup paksa posisi: {e}")
        try:
            _market_order(symbol, exit_side, quantity, reduce_only=True)
        except OrderError as e2:
            print(f"[executor] {symbol}: PERINGATAN -- penutupan paksa JUGA gagal, "
                  f"posisi mungkin masih terbuka tanpa stop: {e2}")
        raise
    return {"entry_order_id": entry.get("orderId"), "stop_order_id": stop.get("orderId"),
            "quantity": quantity, "callback_pct": callback_pct}
