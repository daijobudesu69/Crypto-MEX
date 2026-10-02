"""Offline tests for the OOS tools (docs/OOS_PLAN.md). No network."""
import io
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
import oos_binance_pre2023 as ob  # noqa: E402
import oos_common as oc  # noqa: E402
import oos_selection as osel  # noqa: E402
from mex import datafeed  # noqa: E402
from mex.strategy import Params  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + str(detail)}")


def tr(sym, entry, exit_, r, side=1):
    return {"symbol": sym, "side": side, "entry_time": pd.Timestamp(entry, tz="UTC").isoformat(),
            "exit_time": pd.Timestamp(exit_, tz="UTC").isoformat(), "net_R": float(r),
            "gross_R": float(r), "cost_R": 0.0, "funding_R": 0.0, "bars_held": 1,
            "entry_price": 100.0, "r_price": 2.0}


def test_universe():
    check("24 koin, 12 + 12", len(oc.ELIGIBLE) == 24 and len(oc.GROUP_L) == 12 and len(oc.GROUP_O) == 12)
    check("L dan O tidak tumpang tindih", not set(oc.GROUP_L) & set(oc.GROUP_O))
    live = {v["hyperliquid"][0] for v in datafeed.INSTRUMENTS.values()}
    check("L = koin live minus HYPE", set(oc.GROUP_L.values()) == live - {"HYPE"},
          sorted(live ^ set(oc.GROUP_L.values())))
    check("koin yang dikeluarkan tidak ada di universe", not set(oc.EXCLUDED) & set(oc.ELIGIBLE))
    s = pd.read_csv(os.path.join(ROOT, "backtest", "hyperliquid", "summary.csv"))
    check("24 + 6 = 30 koin backtest", set(oc.ELIGIBLE) | set(oc.EXCLUDED) == set(s["symbol"]))
    ok = s[s["symbol"].isin(oc.ELIGIBLE)]
    check("semua koin eligible mulai <= 2024-06-16 tanpa celah",
          (ok["start"].str[:10] <= "2024-06-16").all() and (ok["gaps"] == 0).all())


def test_in_window():
    t = [tr("A", "2025-01-01", "2025-01-02", 1), tr("A", "2025-01-31", "2025-02-02", 1),
         tr("A", "2025-02-01", "2025-02-02", 1), tr("A", "2024-12-31", "2025-01-01", 1)]
    s, e = pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp("2025-02-01", tz="UTC")
    check("terbuka: masuk di jendela, keluar boleh sesudahnya", len(oc.in_window(t, s, e, False)) == 2)
    check("tertutup: harus keluar sebelum akhir jendela", len(oc.in_window(t, s, e, True)) == 1)
    check("end=None sampai akhir data", len(oc.in_window(t, s, None, True)) == 3)


def test_stats_helpers():
    d, t = oc.welch([1, 1.2, 0.8, 1.1], [0, 0.1, -0.1, 0.05])
    check("welch selisih", np.isclose(d, 1.025 - 0.0125))
    check("welch t besar untuk beda jelas", t > 5)
    check("welch sampel terlalu kecil -> nan", np.isnan(oc.welch([1], [1, 2])[0]))
    check("t_stat", np.isclose(oc.t_stat([1, 2, 3]), 2 / 1 * np.sqrt(3)))
    trades = [tr("A", "2025-01-05", "2025-01-06", 1), tr("B", "2025-01-07", "2025-01-08", 2),
              tr("A", "2025-02-05", "2025-02-06", -1), tr("A", "2025-03-05", "2025-03-06", 3)]
    mt, n = oc.monthly_t(trades)
    check("monthly_t menjumlah per bulan entry", n == 3 and np.isclose(mt, oc.t_stat([3, -1, 3])))
    check("spearman monoton = 1", np.isclose(oc.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0))
    check("spearman terbalik = -1", np.isclose(oc.spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0))


def test_verdicts():
    check("selisih <= 0 -> GAGAL", oc.verdict_diff(-0.01, 3) == "GAGAL" and oc.verdict_diff(0.0, 3) == "GAGAL")
    check("selisih > 0, t < 2 -> TIDAK JELAS", oc.verdict_diff(0.1, 1.99) == "TIDAK JELAS")
    check("selisih > 0, t >= 2 -> LULUS", oc.verdict_diff(0.1, 2.0) == "LULUS")
    check("selisih nan -> GAGAL", oc.verdict_diff(np.nan, np.nan) == "GAGAL")
    check("edge <= 0 -> GAGAL", oc.verdict_edge(-0.01, 5, 1.0) == "GAGAL")
    check("edge lulus semua syarat", oc.verdict_edge(0.1, 2.0, 0.60) == "LULUS")
    check("edge t kurang -> TIDAK JELAS", oc.verdict_edge(0.1, 1.5, 0.9) == "TIDAK JELAS")
    check("edge koin positif < 60% -> TIDAK JELAS", oc.verdict_edge(0.1, 3.0, 0.59) == "TIDAK JELAS")


def synthetic(sel_good, test_good):
    """One trade per coin per month. Coins in sel_good win in 2025-H1, test_good in 2025-H2."""
    out = []
    for sym in oc.ELIGIBLE:
        for m in range(1, 13):
            good = sym in (sel_good if m <= 6 else test_good)
            out.append(tr(sym, f"2025-{m:02d}-05", f"2025-{m:02d}-06", 1.0 if good else -0.5))
    return out


def test_selection():
    syms = list(oc.ELIGIBLE)
    good = set(syms[-12:])        # deliberately not the first 12, so order cannot fake it
    t = synthetic(good, good)
    rk = osel.rank_coins(t, pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp("2025-07-01", tz="UTC"), 1.0)
    check("top 12 = koin yang menang di jendela seleksi", set(rk.loc[rk.group == "top", "symbol"]) == good)
    check("ranking 1..24 tanpa duplikat", sorted(rk["sel_rank"]) == list(range(1, 25)))
    row, test_tr, _ = osel.evaluate(t, ("2025-01-01", "2025-07-01"), ("2025-07-01", None), 1.0, "X")
    check("edge persisten -> top > bottom", row["diff_R"] > 0 and np.isclose(row["top_exp_R"], 1.0))
    check("jendela uji hanya entry di dalamnya", all(x["entry_time"] >= "2025-07-01" for x in test_tr))
    check("spearman positif kalau ranking bertahan", row["spearman"] > 0.5)
    s = osel.summarise(test_tr, 1.0)
    check("summarise: persisten -> bukan GAGAL", s["verdict"] != "GAGAL" and s["diff"] > 0)
    flip = synthetic(good, set(syms) - good)
    _, test_tr2, _ = osel.evaluate(flip, ("2025-01-01", "2025-07-01"), ("2025-07-01", None), 1.0, "Y")
    check("ranking terbalik -> GAGAL", osel.summarise(test_tr2, 1.0)["verdict"] == "GAGAL")


def test_selection_ignores_unfinished():
    t = synthetic(set(), set())
    # A huge winner entered inside the selection window but exiting after it must not count.
    t.append(tr("WLD", "2025-06-30", "2025-07-02", 50.0))
    rk = osel.rank_coins(t, pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp("2025-07-01", tz="UTC"), 1.0)
    check("transaksi yang keluar sesudah jendela seleksi diabaikan",
          int(rk.loc[rk.symbol == "WLD", "sel_rank"].iloc[0]) > 1)


def test_walk_forward_spec():
    w = osel.WALK_FORWARD
    check("5 jendela", len(w) == 5)
    check("jendela uji tidak tumpang tindih & bersambung",
          all(w[i][2] == w[i + 1][1] for i in range(len(w) - 1)))
    check("seleksi = 12 bulan sebelum uji",
          all(pd.Timestamp(s1) - pd.DateOffset(months=12) == pd.Timestamp(s0) for s0, s1, _ in w))
    check("jendela terakhir sampai akhir data", w[-1][2] is None)


def zip_bytes(text):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("x.csv", text)
    return b.getvalue()


def test_binance_helpers():
    check("SHIB -> 1000SHIBUSDT", ob.binance_symbol("SHIB") == "1000SHIBUSDT")
    check("ETH -> ETHUSDT", ob.binance_symbol("ETH") == "ETHUSDT")
    m = ob.months()
    check("bulan 2020-01 .. 2023-06 = 42", len(m) == 42 and m[0] == "2020-01" and m[-1] == "2023-06")
    f = ob.read_funding_zip(zip_bytes("calc_time,funding_interval_hours,last_funding_rate\n"
                                      "1577836800000,8,0.0001\n1577865600000,8,-0.0002\n"))
    check("funding dengan header", len(f) == 2 and np.isclose(f["rate"].iloc[1], -0.0002))
    check("funding waktu UTC", f["time"].iloc[0] == pd.Timestamp("2020-01-01", tz="UTC"))
    g = ob.read_funding_zip(zip_bytes("1577836800000,8,0.0001\n"))
    check("funding tanpa header", len(g) == 1 and np.isclose(g["rate"].iloc[0], 0.0001))


def test_download_cache():
    """A cached coin is read back without touching the network."""
    with tempfile.TemporaryDirectory() as d:
        k = pd.DataFrame({"ts": pd.date_range("2020-01-01", periods=3, freq="4h", tz="UTC"),
                          "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0})
        k.to_csv(os.path.join(d, "ETHUSDT_4h.csv.gz"), index=False)
        pd.DataFrame({"time": ["2020-01-01T08:00:00+00:00"], "rate": [0.0001]}).to_csv(
            os.path.join(d, "ETHUSDT_funding.csv.gz"), index=False)
        with open(os.path.join(d, "ETHUSDT_months.json"), "w") as fh:
            fh.write('{"klines": ["2020-01"], "funding": ["2020-01"]}')
        df, rates, km, fm = ob.download("ETHUSDT", d, refresh=False)
        check("cache candle terbaca", len(df) == 3 and str(df["ts"].dt.tz) == "UTC")
        check("cache funding terbaca", len(rates) == 1 and rates.index.tz is not None)
        check("cache bulan terbaca", km == ["2020-01"] and fm == ["2020-01"])


def test_run_coin_window():
    df = pd.read_csv(os.path.join(FIX, "eth_4h_binance_perp.csv"))
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    p = Params()
    every, _ = bh.run_backtest(df, p, ob.FEE_PCT, ob.SLIP_PCT, "ETH")
    old = ob.WIN_START, ob.WIN_END
    ob.WIN_START, ob.WIN_END = pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp("2025-07-01", tz="UTC")
    try:
        got, _ = ob.run_coin("ETH", df, pd.Series(dtype=float), p)
    finally:
        ob.WIN_START, ob.WIN_END = old
    want = [t for t in every if "2025-01-01" <= t["entry_time"][:10] < "2025-07-01"]
    check("run_coin = run_backtest yang dipotong per entry", len(got) == len(want) and len(got) > 0,
          (len(got), len(want)))
    check("biaya Binance 0,05% dipakai", np.isclose(
        got[0]["cost_R"], -(0.06 / 100) * (got[0]["entry_price"] + got[0]["exit_price"]) / got[0]["r_price"]))


if __name__ == "__main__":
    print("test_oos.py")
    for t in (test_universe, test_in_window, test_stats_helpers, test_verdicts, test_selection,
              test_selection_ignores_unfinished, test_walk_forward_spec, test_binance_helpers,
              test_download_cache, test_run_coin_window):
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    sys.exit(1 if FAIL else 0)
