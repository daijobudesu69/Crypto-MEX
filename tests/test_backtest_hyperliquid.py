"""Offline tests for tools/backtest_hyperliquid.py. No network."""
import os
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import mex.compat  # noqa: F401,E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import backtest_hyperliquid as bh  # noqa: E402
from mex.strategy import Params, compute_features, step  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + str(detail)}")


def fixture():
    df = pd.read_csv(os.path.join(FIX, "eth_4h_binance_perp.csv"))
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def mk(cid, sym, rank, price=1.0, name=None):
    return {"id": cid, "symbol": sym, "name": name or cid, "market_cap_rank": rank,
            "market_cap": 10**12 / rank, "current_price": price}


def test_perp_map():
    meta = {"universe": [{"name": "ETH"}, {"name": "kPEPE"}, {"name": "kSHIB"},
                         {"name": "TON", "isDelisted": True}, {"name": "GRAM"},
                         {"name": "kaito"}]}
    m = bh.hl_perp_map(meta)
    check("k-prefix dipetakan balik ke ticker asli", m.get("PEPE") == "kPEPE" and m.get("SHIB") == "kSHIB")
    check("perp delisted tidak masuk", "TON" not in m)
    check("nama huruf kecil bukan k-prefix", m.get("KAITO") == "kaito")


def test_exclusions():
    stable_ids = {"tether", "usd-coin"}
    r = lambda cid, sym, name=None: bh.exclusion_reason(mk(cid, sym, 1, name=name), stable_ids)  # noqa: E731
    check("BTC dikecualikan", r("bitcoin", "btc") == "btc")
    check("WBTC dikecualikan sebagai BTC", r("wrapped-bitcoin", "wbtc") == "btc")
    check("cbBTC dikecualikan sebagai BTC", r("coinbase-wrapped-btc", "cbbtc") == "btc")
    check("BCH tetap ikut", r("bitcoin-cash", "bch") is None)
    check("USDT dari kategori stablecoin", r("tether", "usdt") == "stablecoin")
    check("USDe lewat daftar tambahan", r("ethena-usde", "usde") == "stablecoin")
    check("USDY (dolar berbunga) dikecualikan", r("ondo-us-dollar-yield", "usdy") == "stablecoin")
    check("XAUT dikecualikan", r("tether-gold", "xaut") == "gold-backed")
    check("wstETH dikecualikan", r("wrapped-steth", "wsteth") == "wrapped/staked derivative")
    check("JitoSOL dikecualikan", r("jito-staked-sol", "jitosol") == "wrapped/staked derivative")
    check("ETH ikut", r("ethereum", "eth") is None)


def test_select_universe():
    markets = [mk("solana", "sol", 5, 150), mk("bitcoin", "btc", 1, 60000),
               mk("ethereum", "eth", 2, 3000), mk("tether", "usdt", 3, 1),
               mk("leo-token", "leo", 4, 9), mk("shiba-inu", "shib", 6, 0.000006),
               mk("fake-sui", "sui", 7, 50.0), mk("sui", "sui", 8, 3.0),
               mk("dogecoin", "doge", 9, 0.2), mk("doge-clone", "doge", 10, 0.2),
               mk("near", "near", 11, 2.0), mk("aptos", "apt", 12, 5.0)]
    perps = {"ETH": "ETH", "SOL": "SOL", "SHIB": "kSHIB", "SUI": "SUI", "DOGE": "DOGE",
             "NEAR": "NEAR", "APT": "APT"}
    mids = {"ETH": "3001", "SOL": "150.5", "kSHIB": "0.006", "SUI": "3.01", "DOGE": "0.2",
            "NEAR": "2.0", "APT": "5.0"}
    chosen, rows = bh.select_universe(markets, {"tether"}, perps, 6, mids)
    syms = [c["symbol"] for c in chosen]
    check("urut market cap, BTC/stable/tanpa-perp dilewati",
          syms == ["ETH", "SOL", "SHIB", "SUI", "DOGE", "NEAR"], syms)
    check("SHIB dipasangkan ke kSHIB (harga /1000 cocok)", chosen[2]["hl_coin"] == "kSHIB")
    status = {r["id"]: r["status"] for r in rows}
    check("ticker bentrok ditolak lewat harga", status["fake-sui"].startswith("skip: ticker bentrok"), status["fake-sui"])
    check("koin asli dengan ticker itu tetap terpilih", status["sui"] == "selected", status["sui"])
    check("ticker yang sama tidak dipakai dua kali", status["doge-clone"].startswith("skip: ticker sudah"),
          status["doge-clone"])
    check("tiap koin yang dilewati punya alasan", all(r["status"] for r in rows))
    check("berhenti tepat di N", len(chosen) == 6 and "aptos" not in status)


def test_parse_candles():
    now = pd.Timestamp("2026-01-01 12:30", tz="UTC")
    ms = lambda s: int(pd.Timestamp(s, tz="UTC").timestamp() * 1000)  # noqa: E731
    row = lambda t, c: {"t": ms(t), "o": "1", "h": "2", "l": "0.5", "c": str(c), "v": "10"}  # noqa: E731
    raw = [row("2026-01-01 04:00", 1.1), row("2026-01-01 00:00", 1.0),
           row("2026-01-01 04:00", 1.2), row("2026-01-01 08:00", 1.3),
           row("2026-01-01 12:00", 1.4)]
    df = bh.parse_candles(raw, now)
    check("bar yang belum tutup dibuang", df["ts"].iloc[-1] == pd.Timestamp("2026-01-01 08:00", tz="UTC"))
    check("duplikat dibuang, nilai terakhir dipakai", len(df) == 3 and df["close"].iloc[1] == 1.2)
    check("urut waktu", df["ts"].is_monotonic_increasing)
    check("gap_count 0 untuk seri rapat", bh.gap_count(df) == 0)
    check("gap_count mendeteksi bar hilang", bh.gap_count(df.drop(index=1)) == 1)


def test_backtest_uses_live_step():
    df, p = fixture(), Params()
    trades, _ = bh.run_backtest(df, p, 0.0, 0.0, "ETH")
    f, ts = compute_features(df, p), pd.DatetimeIndex(df["ts"])
    pos = pend = None
    exits = 0
    for i in range(len(df)):
        pos, pend, ev = step(f, ts, i, p, pos, pend)
        exits += sum(e["event"] == "EXIT" for e in ev)
    check("jumlah transaksi = jumlah EXIT dari step() live", len(trades) == exits, (len(trades), exits))
    check("fixture menghasilkan transaksi", len(trades) > 20, len(trades))
    ok = all(np.isclose(t["gross_R"], t["side"] * (t["exit_price"] - t["entry_price"]) / t["r_price"])
             for t in trades)
    check("gross_R = arah x (exit - entry) / 1R", ok)
    check("tanpa biaya, net = gross", all(t["net_R"] == t["gross_R"] for t in trades))
    check("transaksi tidak tumpang tindih",
          all(pd.Timestamp(a["exit_time"]) <= pd.Timestamp(b["entry_time"])
              for a, b in zip(trades, trades[1:])))
    check("rugi terbesar tanpa biaya tidak jauh lewat 1R (gap open saja)",
          min(t["gross_R"] for t in trades) > -1.6, min(t["gross_R"] for t in trades))

    costed, _ = bh.run_backtest(df, p, 0.045, 0.01, "ETH")
    frac = 0.055 / 100
    ok = all(np.isclose(c["net_R"], t["gross_R"] - frac * (t["entry_price"] + t["exit_price"]) / t["r_price"])
             for c, t in zip(costed, trades))
    check("biaya = (fee+slip) x (entry+exit) / 1R", ok)
    check("biaya tidak mengubah transaksi", [t["entry_time"] for t in trades] == [c["entry_time"] for c in costed])


def test_stats():
    tr = [{"net_R": r, "bars_held": 5} for r in (1.0, -1.0, 2.0, -1.0, -1.0, 3.0)]
    s = bh.stats(tr, 1.0)
    check("win rate", np.isclose(s["win_rate"], 0.5))
    check("expectancy", np.isclose(s["exp_R"], 0.5))
    check("profit factor", np.isclose(s["profit_factor"], 2.0))
    check("max DD dalam R dihitung dari puncak", np.isclose(s["max_dd_R"], 2.0), s["max_dd_R"])
    eq = np.prod([1.01, 0.99, 1.02, 0.99, 0.99, 1.03])
    check("return @1% dimajemukkan", np.isclose(s["return_pct"], (eq - 1) * 100))
    s0 = bh.stats([{"net_R": -1.0, "bars_held": 1}], 1.0)
    check("DD dihitung dari modal awal walau transaksi pertama rugi", np.isclose(s0["max_dd_R"], 1.0))
    check("tanpa transaksi tidak error", bh.stats([], 1.0)["trades"] == 0)
    # 1R = 2% of price: +1R = +2%, -1R = -2%.
    tp = [{"net_R": r, "bars_held": 1, "r_price": 2.0, "entry_price": 100.0} for r in (1.0, -1.0, 1.5)]
    sp = bh.stats(tp, 1.0)
    check("% per transaksi = R x 1R / entry", np.isclose(sp["avg_trade_pct"], (2 - 2 + 3) / 3))
    check("profit modal penuh dimajemukkan", np.isclose(sp["return_1x_pct"], (1.02 * 0.98 * 1.03 - 1) * 100))
    check("max DD modal penuh", np.isclose(sp["max_dd_1x_pct"], 2.0))


def test_funding():
    idx = pd.date_range("2026-01-01 00:00", periods=10, freq="1h", tz="UTC")
    rates = pd.Series(0.0001, index=idx)
    t = {"side": 1, "entry_time": "2026-01-01T02:00:00+00:00",
         "exit_time": "2026-01-01T06:00:00+00:00", "entry_price": 100.0, "r_price": 2.0}
    check("long bayar funding positif (4 jam, (masuk, keluar])",
          np.isclose(bh.funding_r(t, rates), -4 * 0.0001 * 100 / 2))
    check("short terima funding positif", np.isclose(bh.funding_r({**t, "side": -1}, rates), 4 * 0.0001 * 100 / 2))
    check("tanpa data funding = 0", bh.funding_r(t, None) == 0.0)


def test_portfolio():
    tr = [{"exit_time": "2026-01-01T00:00:00+00:00", "net_R": 2.0},
          {"exit_time": "2026-01-01T00:00:00+00:00", "net_R": -1.0},
          {"exit_time": "2026-01-02T00:00:00+00:00", "net_R": -3.0},
          {"exit_time": "2026-01-03T00:00:00+00:00", "net_R": 1.0}]
    c = bh.portfolio_curve(tr, 1.0)
    check("exit bersamaan dijumlah", len(c) == 3 and np.isclose(c["pnl_pct"].iloc[0], 1.0))
    check("equity akhir", np.isclose(c["equity_pct"].iloc[-1], -1.0))
    check("DD portofolio dari puncak", np.isclose(c["dd_pct"].max(), 3.0))


def test_binance_archive():
    with tempfile.TemporaryDirectory() as d:
        folder = os.path.join(d, "ETHUSDT")
        os.makedirs(folder)
        t0 = int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)
        step_ms = 4 * 3600 * 1000
        body = lambda k: "\n".join(f"{t0 + (k + j) * step_ms},1,2,0.5,1.5,10,0,0,0,0,0,0" for j in range(2))  # noqa: E731
        with zipfile.ZipFile(os.path.join(folder, "ETHUSDT-4h-2026-01.zip"), "w") as z:
            z.writestr("a.csv", body(0))
        with zipfile.ZipFile(os.path.join(folder, "ETHUSDT-4h-2026-02.zip"), "w") as z:
            z.writestr("b.csv", "open_time,open,high,low,close,volume,a,b,c,d,e,f\n" + body(2))
        df = bh.load_binance_archive(d, "ETH")
        check("arsip dengan dan tanpa header terbaca", df is not None and len(df) == 4)
        check("kolom harga float", df is not None and df["close"].dtype == float)
        check("simbol tanpa arsip -> None", bh.load_binance_archive(d, "XRP") is None)


if __name__ == "__main__":
    print("test_backtest_hyperliquid.py")
    for t in (test_perp_map, test_exclusions, test_select_universe, test_parse_candles,
              test_backtest_uses_live_step, test_stats, test_funding, test_portfolio,
              test_binance_archive):
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    sys.exit(1 if FAIL else 0)
