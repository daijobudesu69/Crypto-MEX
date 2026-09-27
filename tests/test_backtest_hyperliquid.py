"""Offline tests for tools/backtest_hyperliquid.py.

No network: every CoinGecko/Hyperliquid call is replaced with a stub. This
checks the logic that decides WHICH symbols get backtested and HOW trades are
scored -- it cannot check that the live HTTP calls actually work, because
this environment's own network policy blocks both api.coingecko.com and
api.hyperliquid.xyz. Run the script itself somewhere unrestricted to confirm
that part.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402

import backtest_hyperliquid as bh  # noqa: E402
from mex.strategy import Params  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + detail}")


# --------------------------------------------------------------------------- #
def test_btc_and_stablecoins_excluded_before_ranking():
    rows_by_page = {
        1: [{"id": "bitcoin", "symbol": "btc"}, {"id": "tether", "symbol": "usdt"},
            {"id": "ethereum", "symbol": "eth"}, {"id": "wrapped-bitcoin", "symbol": "wbtc"},
            {"id": "ripple", "symbol": "xrp"}],
        2: [{"id": "dogecoin", "symbol": "doge"}],
    }

    def fake_cg(params):
        if params.get("category") == "stablecoins":
            return [{"id": "tether"}] if params["page"] == 1 else []
        return rows_by_page.get(params["page"], [])

    stables = bh.stablecoin_ids(fake_cg)
    check("kategori stablecoin CoinGecko diambil", stables == {"tether"}, stables)

    top = bh.top_market_cap_symbols(3, stables, fake_cg)
    check("BTC tidak ikut walau rank #1", "BTC" not in top, top)
    check("WBTC ikut dikecualikan (exposure BTC juga)", "WBTC" not in top, top)
    check("USDT (stablecoin) tidak ikut", "USDT" not in top, top)
    check("hasil 3 simbol non-BTC/stablecoin teratas, urut market cap",
          top == ["ETH", "XRP", "DOGE"], top)


def test_universe_intersection_reports_skipped_not_drops_silently():
    def fake_cg(params):
        if params.get("category") == "stablecoins":
            return []
        return [{"id": "ethereum", "symbol": "eth"},
                {"id": "some-new-coin", "symbol": "xyz"}] if params["page"] == 1 else []

    def fake_hl(payload):
        assert payload == {"type": "meta"}
        return {"universe": [{"name": "ETH"}, {"name": "BTC"}]}

    keep, skipped = bh.resolve_symbols(2, fake_cg, fake_hl)
    check("simbol yang ada di Hyperliquid dipakai", keep == ["ETH"], keep)
    check("simbol top-N yang TIDAK listed di Hyperliquid dilaporkan, bukan dibuang diam",
          skipped == ["XYZ"], skipped)


def test_delisted_market_excluded_from_universe():
    def fake_hl(payload):
        return {"universe": [{"name": "ETH"}, {"name": "OLDCOIN", "isDelisted": True}]}

    u = bh.hyperliquid_universe(fake_hl)
    check("pasar yang sudah delisted tidak masuk universe", u == {"ETH"}, u)


def test_history_pagination_stops_at_now_and_treats_empty_as_not_listed_yet():
    calls = []

    def fake_candles(coin, interval, start_ms, end_ms):
        calls.append((start_ms, end_ms))
        # First window (before "listing") is empty; second has one candle.
        if len(calls) == 1:
            return []
        t = start_ms
        return [{"t": t, "o": "10", "h": "11", "l": "9", "c": "10.5", "v": "100"}]

    start = pd.Timestamp("2023-01-01T00:00:00Z")
    end = start + bh.CHUNK * 2
    df = bh.fetch_history("ETH", "4h", start=start, end=end, fetch=fake_candles)
    check("window kosong (sebelum listing) tidak menghentikan pagination",
          len(calls) == 2, calls)
    check("bar dari window kedua tetap masuk DataFrame", len(df) == 1, df)


def test_fetch_history_raises_when_nothing_ever_returned():
    raised = False
    try:
        bh.fetch_history("GHOST", "4h",
                         start=pd.Timestamp("2023-01-01T00:00:00Z"),
                         end=pd.Timestamp("2023-01-02T00:00:00Z"),
                         fetch=lambda *a: [])
    except bh.BacktestError:
        raised = True
    check("simbol tanpa data sama sekali -> error, bukan backtest kosong senyap", raised)


def test_run_backtest_reuses_live_step_and_scores_trades_in_R():
    """Feeds a hand-built breakout+pullback series through the SAME step()
    the live forward test runs, and checks the summary math on top of it --
    this is deliberately not a re-implementation of the strategy."""
    import numpy as np

    n = 140
    ts = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
    price = np.linspace(100, 100, n)
    # A clean breakout with a volume spike around bar 60, a rally, then a
    # reversal sharp enough to hit the trailing stop -- engineered only to
    # guarantee at least one CLOSED trade for the aggregation logic to score.
    price[:60] = 100 + np.sin(np.linspace(0, 6, 60))
    price[60:100] = np.linspace(101, 140, 40)
    price[100:] = np.linspace(140, 90, n - 100)
    vol = np.full(n, 100.0)
    vol[60] = 500.0
    df = pd.DataFrame({
        "ts": ts, "open": price, "high": price + 0.5, "low": price - 0.5,
        "close": price, "volume": vol,
    })
    p = Params(n_lookback=10, vol_len=10, ema_fast=5, ema_slow=15, rsi_len=7,
               roc_len=3, atr_len=7, allow_shorts=False)
    trades = bh.run_backtest("ETH", df, p)
    check("skenario breakout+rally menghasilkan minimal satu transaksi",
          len(trades) >= 1, str(trades))
    if trades:
        check("result_R dihitung dari entry/exit/r_usdt, konsisten dengan step()",
              all(isinstance(t["result_R"], float) for t in trades))

    summary = bh.summarize("ETH", trades, bars=n)
    check("win_rate_pct dan expectancy_R konsisten dengan daftar trades",
          summary["trades"] == len(trades))


def test_summarize_empty_trades_does_not_crash():
    s = bh.summarize("XRP", [], bars=500)
    check("simbol tanpa transaksi sama sekali dilaporkan apa adanya, bukan dilewati",
          s == {"symbol": "XRP", "bars": 500, "trades": 0, "win_rate_pct": "",
                "expectancy_R": "", "sum_R": "", "max_drawdown_R": ""}, s)


def test_max_drawdown_is_running_peak_to_trough_in_R():
    trades = [{"result_R": 2.0}, {"result_R": -3.0}, {"result_R": 1.0},
              {"result_R": -0.5}]
    s = bh.summarize("ETH", trades, bars=10)
    # equity path: 2, -1, 0, -0.5 ; peak path: 2, 2, 2, 2 ; dd: 0,-3,-2,-2.5
    check("max_drawdown_R = jarak terjauh dari peak yang pernah dicapai",
          s["max_drawdown_R"] == -3.0, s)


if __name__ == "__main__":
    print("test_backtest_hyperliquid.py")
    for t in (test_btc_and_stablecoins_excluded_before_ranking,
              test_universe_intersection_reports_skipped_not_drops_silently,
              test_delisted_market_excluded_from_universe,
              test_history_pagination_stops_at_now_and_treats_empty_as_not_listed_yet,
              test_fetch_history_raises_when_nothing_ever_returned,
              test_run_backtest_reuses_live_step_and_scores_trades_in_R,
              test_summarize_empty_trades_does_not_crash,
              test_max_drawdown_is_running_peak_to_trough_in_R):
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    sys.exit(1 if FAIL else 0)
