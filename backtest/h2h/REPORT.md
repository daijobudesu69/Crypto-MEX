# Head-to-head — Hyperliquid vs Binance perp, koin forward test

Dibuat 2026-09-27 14:53 UTC oleh `tools/h2h_hl_binance.py`. Parameter = `config.yaml`. Kedua venue dipotong ke **himpunan bar 4H yang persis sama** (irisan timestamp) — jendela, jumlah bar dan warmup identik; yang beda hanya sumber candle.

Biaya: fee taker tier 0 masing-masing (Hyperliquid 0.045%, Binance 0.05%) + slippage 0.01% per sisi. **Funding tidak dihitung di kedua sisi** — arsip funding Binance hanya bulanan, jadi bulan berjalan akan hilang di satu sisi saja. Di backtest 30 koin, funding Hyperliquid rata-rata cuma -0.007 R/trx.

## Hasil per koin

| Koin | Venue | Trx | Win | Rata2/trx | **Profit @risiko 1%** | Max DD @1% | **Profit modal penuh 1x** | Max DD 1x | Exp R | PF |
|---|---|---|---|---|---|---|---|---|---|---|
| ETH | Hyperliquid | 96 | 42.7% | +0.39% | **+22.6%** | 10.3% | **+36.7%** | 23.8% | +0.223 | 1.61 |
|  | Binance | 96 | 49.0% | +0.72% | **+32.4%** | 7.6% | **+87.1%** | 15.8% | +0.303 | 1.96 |
| DOGE | Hyperliquid | 100 | 47.0% | +1.41% | **+47.5%** | 3.7% | **+246.1%** | 18.0% | +0.401 | 2.28 |
|  | Binance | 88 | 45.5% | +1.64% | **+45.8%** | 4.4% | **+255.8%** | 12.3% | +0.441 | 2.36 |
| XRP | Hyperliquid | 89 | 47.2% | +1.56% | **+37.8%** | 5.2% | **+246.3%** | 14.2% | +0.373 | 2.14 |
|  | Binance | 87 | 46.0% | +0.96% | **+23.6%** | 5.0% | **+112.1%** | 17.9% | +0.254 | 1.76 |
| SOL | Hyperliquid | 94 | 42.6% | +0.48% | **+19.1%** | 6.8% | **+47.7%** | 23.6% | +0.193 | 1.58 |
|  | Binance | 95 | 44.2% | +0.60% | **+21.5%** | 9.9% | **+63.9%** | 32.3% | +0.213 | 1.62 |
| **GABUNGAN** | Hyperliquid | 379 | 44.9% | +0.96% | **+196.9%** | 13.6% | **+144.2%** | 8.9% | +0.298 | 1.89 |
|  | Binance | 366 | 46.2% | +0.97% | **+190.0%** | 12.8% | **+129.7%** | 11.0% | +0.301 | 1.92 |

`GABUNGAN`: kolom risiko 1% = semua transaksi 4 koin berurutan menurut waktu exit, tiap transaksi risiko 1% modal. Kolom modal penuh 1x = modal dibagi rata 25% per koin (tidak bisa 100% ke tiap koin karena posisinya sering terbuka bersamaan).

## Periode dan kualitas data

| Koin | Dari | Sampai | Bars (sama) | Bar hilang HL / BN | Beda close (median) | Beda ATR (median) |
|---|---|---|---|---|---|---|
| ETH | 2024-06-16 04:00 | 2026-09-26 20:00 | 4997 | 0 / 0 | 0.023% | 2.05% |
| DOGE | 2024-06-16 04:00 | 2026-09-26 20:00 | 4997 | 0 / 0 | 0.032% | 2.42% |
| XRP | 2024-06-16 08:00 | 2026-09-26 20:00 | 4996 | 0 / 0 | 0.027% | 1.56% |
| SOL | 2024-06-16 04:00 | 2026-09-26 20:00 | 4997 | 0 / 0 | 0.027% | 1.51% |

## Sinyal — seberapa sama?

| Koin | Sinyal HL | Sinyal BN | Sama persis (bar + arah) | Hanya HL | Hanya BN | Transaksi identik | Hasil searah (untung/rugi sama) | Selisih R rata2 di transaksi identik |
|---|---|---|---|---|---|---|---|---|
| ETH | 173 | 172 | 135 (64%) | 38 | 37 | 69 | 97% | 0.073 R |
| DOGE | 190 | 165 | 138 (64%) | 52 | 27 | 69 | 99% | 0.043 R |
| XRP | 181 | 158 | 137 (68%) | 44 | 21 | 61 | 98% | 0.072 R |
| SOL | 177 | 162 | 138 (69%) | 39 | 24 | 71 | 99% | 0.095 R |

**Kenapa sinyal beda?** Untuk tiap sinyal yang hanya muncul di satu venue, syarat mana yang gagal di venue satunya (satu sinyal bisa gagal >1 syarat):

| Koin | Sinyal hanya di HL — gagal di BN karena | Sinyal hanya di BN — gagal di HL karena |
|---|---|---|
| ETH | volume 34, breakout harga 6 | volume 34, breakout harga 2, RSI 1 |
| DOGE | volume 51, breakout harga 3, tren EMA 1 | volume 26, breakout harga 2, tren EMA 1 |
| XRP | volume 42, breakout harga 3 | volume 18, breakout harga 2, RSI 2 |
| SOL | volume 36, breakout harga 3, RSI 1 | volume 23, breakout harga 1 |
