"""Order sizing for MEX 3.0 on Hyperliquid: isolated margin, fixed risk per trade.

Leverage does not change the risk. The order is sized so a stop-out loses
risk_pct of capital:

    order_usd = capital x risk_pct / (1R / entry)

Leverage only sets how much margin that order locks up (order_usd / leverage)
and, in isolated mode, where the position is liquidated. Isolated also caps the
worst case at the margin: when a crash runs through the stop -- 10 Oct 2025 put
-54% to -67% into a single 4H candle on TAO, MNT and DOGE -- the loss is the
margin, not order_usd x the drop.
"""
from dataclasses import dataclass

import requests

from .datafeed import HYPERLIQUID, INSTRUMENTS, UA

# Hyperliquid max leverage per coin on 2026-09-29. Maintenance margin is half
# the initial margin at max leverage, so this sets the liquidation price.
# tests/test_connectivity.py compares it against the live exchange and fails
# when Hyperliquid changes one.
HL_MAX_LEVERAGE = {
    "ETHUSDT": 25, "XRPUSDT": 20, "SOLUSDT": 20, "DOGEUSDT": 10, "HYPEUSDT": 10,
    "SUIUSDT": 10, "1000SHIBUSDT": 10, "DOTUSDT": 10, "ENAUSDT": 10,
    "LINKUSDT": 10, "NEARUSDT": 10, "TAOUSDT": 5, "MNTUSDT": 5,
}
HL_MIN_ORDER_USD = 10.0


@dataclass
class Sizing:
    order_usd: float
    qty: float
    margin_usd: float
    risk_usd: float
    liq_price: float
    liq_pct: float          # distance from entry, signed (negative for a long)
    leverage: int
    raised_to_min: bool     # order was below $10 and was raised to it (risk_usd grew)
    risk_target_usd: float  # capital x risk_pct, before any raise
    hl_coin: str            # the ticker the order is placed in (kSHIB, not SHIB)
    capital_usd: float
    risk_pct: float
    capital_live: bool = False  # capital_usd is the account's live balance, not config


def live_balance(account: str, timeout: float = 10.0) -> float | None:
    """USDC balance of the account, or None when it cannot be read.

    Public info endpoint: needs only the (already public) account address, never
    the API wallet key. Same field the executor sizes from: the account runs in
    unified mode, where the spot clearinghouse holds the balance.
    """
    try:
        r = requests.post(HYPERLIQUID, json={"type": "spotClearinghouseState", "user": account},
                          timeout=timeout, headers=UA)
        r.raise_for_status()
        for b in r.json().get("balances", []):
            if b.get("coin") == "USDC":
                v = float(b["total"])
                return v if v > 0 else None
    except Exception as e:  # noqa: BLE001
        print(f"[sizing] saldo live tidak terbaca: {type(e).__name__}: {e}")
    return None


def maintenance_rate(symbol: str) -> float:
    return 1.0 / (2.0 * HL_MAX_LEVERAGE[symbol])


def liquidation_price(entry: float, side: int, leverage: float, mm: float) -> float:
    """Hyperliquid isolated liquidation price.

    liq = entry - side x margin_available / size / (1 - mm x side), with
    margin_available = entry x size x (1/leverage - mm).
    """
    return entry - side * entry * (1.0 / leverage - mm) / (1.0 - mm * side)


def size(symbol: str, entry: float, r_price: float, side: int,
         capital_usd: float, risk_pct: float, leverage: int,
         capital_live: bool = False) -> Sizing:
    if leverage > HL_MAX_LEVERAGE[symbol]:
        raise ValueError(f"{symbol}: leverage {leverage}x di atas maksimum "
                         f"Hyperliquid {HL_MAX_LEVERAGE[symbol]}x")
    target = capital_usd * risk_pct / 100.0
    stop_frac = r_price / entry
    order = target / stop_frac
    # Decided 2026-09-29: an order under the exchange minimum is raised to it
    # rather than skipped. Only wide stops (> 10%) hit this -- 0.4% of backtest
    # trades -- and the risk grows to at most ~1.35x target.
    raised = order < HL_MIN_ORDER_USD
    if raised:
        order = HL_MIN_ORDER_USD
    liq = liquidation_price(entry, side, leverage, maintenance_rate(symbol))
    return Sizing(
        order_usd=order, qty=order / entry, margin_usd=order / leverage,
        risk_usd=order * stop_frac, liq_price=liq, liq_pct=(liq / entry - 1.0) * 100.0,
        leverage=leverage, raised_to_min=raised, risk_target_usd=target,
        hl_coin=INSTRUMENTS[symbol]["hyperliquid"][0],
        capital_usd=capital_usd, risk_pct=risk_pct, capital_live=capital_live,
    )
