"""Offline tests for mex/executor.py -- the Binance Futures TESTNET order path.

No network: every test monkeypatches the two seams that would otherwise hit
testnet.binancefuture.com (_signed_request, _public_request) and inspects what
would have been sent. Money-adjacent code gets checked before it is trusted.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mex.compat  # noqa: F401,E402

from mex import executor  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + detail}")


def test_configured_requires_both_secrets():
    old = {k: os.environ.pop(k, None) for k in
           ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET")}
    try:
        check("tanpa secret sama sekali -> not configured", not executor.configured())
        os.environ["BINANCE_TESTNET_API_KEY"] = "k"
        check("hanya key, secret kosong -> masih not configured",
              not executor.configured())
        os.environ["BINANCE_TESTNET_API_SECRET"] = "s"
        check("keduanya ada -> configured", executor.configured())
    finally:
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


def test_round_step_never_rounds_up():
    check("qty dibulatkan KE BAWAH ke step_size (Binance menolak yang tidak)",
          executor._round_step(0.1234, 0.001) == 0.123)
    check("qty yang sudah pas step tidak berubah",
          executor._round_step(0.5, 0.1) == 0.5)
    check("step_size=0 tidak membagi nol", executor._round_step(1.2345, 0) == 1.2345)


def test_size_position_uses_readme_formula_and_step_size():
    executor._filters_cache["ETHUSDT"] = {
        "step_size": 0.001, "min_qty": 0.001, "tick_size": 0.01, "min_notional": 5.0,
    }
    try:
        # README: qty = (risk% x equity) / 1R
        qty = executor.size_position("ETHUSDT", entry_price=2500.0, r_usdt=50.0,
                                     equity=10_000.0, risk_pct=1.0)
        raw = (1.0 / 100.0 * 10_000.0) / 50.0
        check("qty sama dengan formula README sebelum dibulatkan step",
              abs(qty - executor._round_step(raw, 0.001)) < 1e-9, (qty, raw))
    finally:
        executor._filters_cache.pop("ETHUSDT", None)


def test_size_position_rejects_below_exchange_minimum():
    executor._filters_cache["ETHUSDT"] = {
        "step_size": 0.001, "min_qty": 0.001, "tick_size": 0.01, "min_notional": 5.0,
    }
    try:
        raised = False
        try:
            # risk kecil sekali x equity kecil -> qty di bawah min_notional
            executor.size_position("ETHUSDT", entry_price=2500.0, r_usdt=5000.0,
                                   equity=10.0, risk_pct=0.01)
        except executor.OrderError:
            raised = True
        check("posisi di bawah min_notional/min_qty ditolak, bukan dikirim lebih kecil",
              raised)
    finally:
        executor._filters_cache.pop("ETHUSDT", None)


def test_trailing_stop_clamps_callback_rate():
    """Binance rejects callbackRate outside [0.1, 5.0]. The strategy's own
    measured range (README: 1.09%-5.24%) can exceed the top end, so both
    bounds need clamping, not just the low one."""
    sent = {}

    def fake_signed(method, path, params):
        sent.update(params)
        return {"orderId": 1}

    real = executor._signed_request
    executor._signed_request = fake_signed
    try:
        executor._trailing_stop_order("ETHUSDT", "SELL", 1.0, callback_pct=0.03)
        check("callback di bawah 0.1% dinaikkan ke minimum", sent["callbackRate"] == 0.1)
        executor._trailing_stop_order("ETHUSDT", "SELL", 1.0, callback_pct=7.4)
        check("callback di atas 5.0% dipotong ke maksimum", sent["callbackRate"] == 5.0)
        executor._trailing_stop_order("ETHUSDT", "SELL", 1.0, callback_pct=2.75)
        check("callback normal dibulatkan 1 desimal, bukan diubah nilainya",
              sent["callbackRate"] == 2.8 or sent["callbackRate"] == 2.7,
              sent["callbackRate"])
        check("stop order selalu reduceOnly -- tidak boleh membuka posisi baru",
              sent["reduceOnly"] == "true")
    finally:
        executor._signed_request = real


def test_failed_stop_flattens_the_position():
    """If the trailing stop leg fails after entry already filled, the position
    must be closed at market rather than left open and unprotected."""
    calls = []

    def fake_market(symbol, side, quantity, reduce_only):
        calls.append(("market", side, reduce_only))
        return {"orderId": len(calls)}

    def fake_stop(symbol, side, quantity, callback_pct):
        raise executor.OrderError("simulated exchange rejection")

    real_market, real_stop = executor._market_order, executor._trailing_stop_order
    executor._market_order, executor._trailing_stop_order = fake_market, fake_stop
    try:
        raised = False
        try:
            executor.place_entry_with_stop("ETHUSDT", side=1, quantity=1.0,
                                           callback_pct=2.0)
        except executor.OrderError:
            raised = True
        check("kegagalan stop order tetap dilaporkan sebagai error", raised)
        check("entry (BUY) dan penutupan paksa (SELL, reduceOnly) sama-sama terkirim",
              calls == [("market", "BUY", False), ("market", "SELL", True)], calls)
    finally:
        executor._market_order, executor._trailing_stop_order = real_market, real_stop


def test_cancel_open_orders_never_raises():
    """A stop that already filled has nothing left to cancel -- that is the
    expected outcome on every normal exit, not a failure to surface."""
    def fake_signed(method, path, params):
        raise executor.OrderError("HTTP 400: Unknown order sent")

    real = executor._signed_request
    executor._signed_request = fake_signed
    try:
        executor.cancel_open_orders("ETHUSDT")   # must not raise
        check("cancel_open_orders menelan error tanpa melempar", True)
    finally:
        executor._signed_request = real


if __name__ == "__main__":
    print("test_executor.py")
    for t in (test_configured_requires_both_secrets,
              test_round_step_never_rounds_up,
              test_size_position_uses_readme_formula_and_step_size,
              test_size_position_rejects_below_exchange_minimum,
              test_trailing_stop_clamps_callback_rate,
              test_failed_stop_flattens_the_position,
              test_cancel_open_orders_never_raises):
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    sys.exit(1 if FAIL else 0)
