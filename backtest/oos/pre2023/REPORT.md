# Uji B — MEX di Binance perp, Jan 2020 → Mei 2023 (OOS waktu)

Dibuat 2026-10-02 03:44 UTC oleh `tools/oos_binance_pre2023.py`. Rencana & kriteria: `docs/OOS_PLAN.md` §4, commit `74770a1 2026-10-02T10:37:52+07:00`.

Transaksi yang **masuk** 2020-01-01 → 2023-05-31. Data `data.binance.vision` (USDT-M perp 4H + funding 8 jam). Biaya: fee 0.05% + slippage 0.01% per sisi + funding asli. Parameter = `config.yaml`, tidak diubah.

## B1 — edge strategi: **TIDAK JELAS**

Kriteria: expectancy gabungan > 0, t ≥ 2.0, dan ≥ 60% koin positif → LULUS; expectancy > 0 tapi syarat lain kurang → TIDAK JELAS; ≤ 0 → GAGAL.

| | Trx | Exp R | Win | PF | t | Total R |
|---|---|---|---|---|---|---|
| Semua (18 koin) | 2344 | +0.001 | 36.1% | 1.00 | +0.06 | +3.4 |
| Long | 2037 | +0.001 | 35.8% | 1.00 | +0.04 | +1.8 |
| Fade short | 307 | +0.005 | 37.8% | 1.01 | +0.09 | +1.6 |

- Koin dengan expectancy > 0: **5/18** (28%)
- Sebelum biaya +0.041 R; fee+slippage -0.030 R; funding -0.010 R per trx
- t dari total R per bulan: +0.05 (41 bulan) — transaksi antar koin berkorelasi, jadi ini pembanding yang lebih konservatif dari t per transaksi
- Patokan: backtest Hyperliquid 30 koin +0,105 R; ETH Binance 2023–26 +0,31 R

### Per tahun (tahun entry)

| | Trx | Exp R | Win | PF | t | Total R |
|---|---|---|---|---|---|---|
| 2020 | 535 | +0.120 | 38.7% | 1.32 | +2.19 | +64.3 |
| 2021 | 844 | -0.003 | 35.5% | 0.99 | -0.06 | -2.1 |
| 2022 | 595 | -0.032 | 36.6% | 0.91 | -0.81 | -19.2 |
| 2023 | 370 | -0.107 | 32.7% | 0.75 | -2.10 | -39.5 |

## B2 — kelompok koin live (L) vs lainnya (O): **TIDAK JELAS**

Kriteria sama dengan Uji A: selisih L − O > 0 dan t Welch ≥ 2.0 → LULUS.

| | Trx | Exp R | Win | PF | t | Total R |
|---|---|---|---|---|---|---|
| L — koin live | 1023 | +0.004 | 35.3% | 1.01 | +0.11 | +4.3 |
| O — lainnya | 1321 | -0.001 | 36.7% | 1.00 | -0.02 | -0.8 |

**Selisih L − O: +0.005 R/trx, t Welch +0.10** (t per bulan +0.01, 41 bulan).

Spearman expectancy per koin Binance 2020–23 vs Hyperliquid 2024–26: **+0.15**.

## Per koin

| Koin | Kel. | Status | Binance | Bar | Celah | Bulan funding | Trx | Exp R | t | Profit @1% | Exp R HL 2024–26 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| SUI | L | < 300 bar | SUIUSDT | 170 | 0 | 2/2 | 0 | — | — | +0.0% | +0.214 |
| ADA | O | diuji | ADAUSDT | 7300 | 0 | 42/42 | 139 | +0.228 | +2.28 | +36.0% | -0.047 |
| DOGE | L | diuji | DOGEUSDT | 6334 | 0 | 36/36 | 134 | +0.200 | +1.24 | +27.8% | +0.391 |
| ETH | L | diuji | ETHUSDT | 7482 | 0 | 42/42 | 134 | +0.196 | +1.96 | +28.8% | +0.213 |
| XLM | O | diuji | XLMUSDT | 7336 | 30 | 42/42 | 141 | +0.171 | +1.40 | +25.4% | +0.169 |
| BNB | O | diuji | BNBUSDT | 7240 | 0 | 41/41 | 154 | +0.009 | +0.10 | +0.5% | -0.073 |
| XRP | L | diuji | XRPUSDT | 7420 | 30 | 42/42 | 155 | -0.004 | -0.04 | -1.8% | +0.365 |
| BCH | O | diuji | BCHUSDT | 7482 | 0 | 42/42 | 149 | -0.013 | -0.15 | -2.7% | +0.013 |
| SOL | L | diuji | SOLUSDT | 5909 | 30 | 34/34 | 106 | -0.032 | -0.28 | -4.0% | +0.184 |
| UNI | O | diuji | UNIUSDT | 5915 | 0 | 34/34 | 111 | -0.040 | -0.41 | -4.9% | +0.073 |
| TRX | O | diuji | TRXUSDT | 7366 | 30 | 42/42 | 166 | -0.044 | -0.50 | -8.1% | +0.053 |
| LINK | L | diuji | LINKUSDT | 7384 | 0 | 42/42 | 168 | -0.046 | -0.62 | -8.2% | +0.136 |
| HBAR | O | diuji | HBARUSDT | 4805 | 30 | 28/28 | 87 | -0.074 | -0.73 | -6.6% | +0.078 |
| AVAX | O | diuji | AVAXUSDT | 5885 | 0 | 34/34 | 109 | -0.089 | -1.38 | -9.4% | +0.122 |
| AAVE | O | diuji | AAVEUSDT | 5747 | 0 | 33/33 | 110 | -0.093 | -1.07 | -10.1% | +0.043 |
| SHIB | L | diuji | 1000SHIBUSDT | 4508 | 0 | 26/26 | 79 | -0.093 | -0.75 | -7.5% | +0.195 |
| NEAR | L | diuji | NEARUSDT | 5722 | 30 | 33/33 | 133 | -0.096 | -1.16 | -12.5% | +0.124 |
| LTC | O | diuji | LTCUSDT | 7402 | 30 | 42/42 | 155 | -0.115 | -1.65 | -16.9% | -0.119 |
| DOT | L | diuji | DOTUSDT | 6077 | 0 | 35/35 | 114 | -0.148 | -1.76 | -15.9% | +0.184 |
| TAO | L | tidak ada data | TAOUSDT | 0 | 0 | 0/0 | 0 | — | — | +0.0% | +0.237 |
| MNT | L | tidak ada data | MNTUSDT | 0 | 0 | 0/0 | 0 | — | — | +0.0% | +0.205 |
| ENA | L | tidak ada data | ENAUSDT | 0 | 0 | 0/0 | 0 | — | — | +0.0% | +0.151 |
| ONDO | O | tidak ada data | ONDOUSDT | 0 | 0 | 0/0 | 0 | — | — | +0.0% | +0.001 |
| WLD | O | tidak ada data | WLDUSDT | 0 | 0 | 0/0 | 0 | — | — | +0.0% | +0.118 |

## File

- `summary.csv` — tabel per koin
- `trades.csv` — semua transaksi di jendela
- `data/` — cache arsip Binance (tidak di-commit; hapus atau `--refresh` untuk unduh ulang)
