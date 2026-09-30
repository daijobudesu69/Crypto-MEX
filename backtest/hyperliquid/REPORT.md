# MEX backtest — Hyperliquid perp, top 30 market cap (tanpa BTC & stablecoin)

Dibuat 2026-09-27 14:49 UTC oleh `tools/backtest_hyperliquid.py`. Parameter = `config.yaml` (tidak diubah). Timeframe 4h. Biaya: fee 0.045% + slippage 0.01% per sisi (taker Hyperliquid tier 0). Funding: **ikut dihitung** (funding per jam asli Hyperliquid).

> **Batas data:** Hyperliquid hanya menyimpan 5.000 candle terakhir per interval. Di 4H itu mulai **2024-06-14** — lebih pendek dari backtest Binance asli (2023-06 → 2026-08). Koin yang listing belakangan punya data lebih sedikit lagi (lihat kolom `Bars`).

## Ringkasan gabungan (semua koin)

| | Semua | Long | Fade short | Patokan ETH Binance |
|---|---|---|---|---|
| Transaksi | 2651 | 2151 | 500 | 118 |
| Win rate | 38.9% | 38.4% | 40.8% | 44.9% |
| Expectancy (net) | +0.105 R | +0.112 R | +0.074 R | +0.31 R |
| Expectancy (sebelum biaya) | +0.147 R | | | |
| — dimakan fee+slippage | -0.035 R | | | |
| — dimakan funding | -0.007 R | | | |
| Total R | +278.7 | +241.7 | +37.0 | |
| Profit factor | 1.28 | 1.30 | 1.20 | 1.95 |
| t-stat | +4.38 | +4.11 | +1.53 | 2.20 |
| Koin dengan expectancy > 0 | **23 / 30** | | | 18 / 21 |

**Portofolio** (semua 30 koin jalan bersamaan, tiap transaksi risiko 1.0% dari modal awal, tidak dimajemukkan): PnL **+278.7%** dari modal awal. Max drawdown **67.6 poin** modal awal = **20.2%** dari ekuitas puncak. Dengan 30 koin terbuka bersamaan, risiko 1% per transaksi berarti eksposur total bisa jauh di atas 1% — koin-koin ini bergerak searah.

## Per koin — profit dalam %

Dua cara membaca % profit dari transaksi yang sama (semua sudah dipotong fee, slippage dan funding):

- **Risiko 1%/trx** — ukuran posisi = 1% modal ÷ 1R, persis cara strategi ini dijalankan. Dimajemukkan.
- **Modal penuh 1x** — seluruh modal masuk tiap transaksi, tanpa leverage. Ini sama dengan % gerak harga yang ditangkap. Dimajemukkan.

| # | Koin | Dari | Trx | Win | Rata2/trx | **Profit @risiko 1%** | Max DD @1% | **Profit modal penuh 1x** | Max DD 1x | Exp R | PF |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 12 | DOGE | 2024-06-16 | 100 | 47.0% | +1.37% | **+46.2%** | 3.8% | **+232.8%** | 18.7% | +0.391 | 2.24 |
| 5 | XRP | 2024-06-16 | 89 | 47.2% | +1.53% | **+36.9%** | 5.3% | **+237.9%** | 14.5% | +0.365 | 2.11 |
| 11 | HYPE | 2024-12-05 | 72 | 43.1% | +1.56% | **+24.7%** | 4.1% | **+169.8%** | 21.5% | +0.316 | 2.16 |
| 33 | TAO | 2024-06-16 | 92 | 47.8% | +0.77% | **+23.6%** | 6.5% | **+81.0%** | 36.8% | +0.237 | 1.81 |
| 50 | MNT | 2024-06-16 | 100 | 45.0% | +0.81% | **+21.9%** | 5.8% | **+101.0%** | 19.6% | +0.205 | 1.55 |
| 2 | ETH | 2024-06-16 | 96 | 42.7% | +0.36% | **+21.4%** | 10.4% | **+33.1%** | 24.0% | +0.213 | 1.58 |
| 26 | SUI | 2024-06-16 | 93 | 40.9% | +0.93% | **+21.0%** | 4.5% | **+105.8%** | 19.8% | +0.214 | 1.64 |
| 7 | SOL | 2024-06-16 | 94 | 41.5% | +0.45% | **+18.1%** | 7.2% | **+43.8%** | 24.5% | +0.184 | 1.55 |
| 34 | SHIB | 2024-06-16 | 85 | 38.8% | +0.65% | **+17.1%** | 8.1% | **+55.9%** | 26.1% | +0.195 | 1.47 |
| 53 | DOT | 2024-06-16 | 86 | 40.7% | +0.53% | **+16.2%** | 6.7% | **+45.5%** | 25.1% | +0.184 | 1.54 |
| 40 | ENA | 2024-06-16 | 99 | 37.4% | +0.47% | **+14.9%** | 9.0% | **+25.6%** | 44.2% | +0.151 | 1.39 |
| 13 | LINK | 2024-06-16 | 105 | 41.9% | +0.45% | **+14.5%** | 9.8% | **+45.1%** | 29.5% | +0.136 | 1.37 |
| 21 | NEAR | 2024-06-16 | 105 | 34.3% | +0.32% | **+12.8%** | 5.8% | **+15.5%** | 31.4% | +0.124 | 1.33 |
| 28 | AVAX | 2024-06-16 | 95 | 43.2% | +0.46% | **+11.7%** | 10.3% | **+44.7%** | 30.9% | +0.122 | 1.36 |
| 52 | WLD | 2024-06-16 | 93 | 35.5% | +0.54% | **+10.8%** | 12.1% | **+35.2%** | 49.5% | +0.118 | 1.31 |
| 20 | XLM | 2024-06-14 | 62 | 48.4% | +0.54% | **+10.5%** | 5.0% | **+28.9%** | 25.8% | +0.169 | 1.56 |
| 9 | ZEC | 2025-04-19 | 53 | 35.8% | +1.13% | **+10.2%** | 3.5% | **+59.4%** | 17.9% | +0.191 | 1.55 |
| 23 | UNI | 2024-06-16 | 104 | 36.5% | +0.35% | **+7.2%** | 6.6% | **+27.6%** | 28.5% | +0.073 | 1.21 |
| 32 | HBAR | 2024-06-16 | 93 | 36.6% | +0.17% | **+6.4%** | 10.5% | **+2.7%** | 36.2% | +0.078 | 1.18 |
| 8 | TRX | 2024-06-16 | 108 | 39.8% | +0.45% | **+5.3%** | 11.0% | **+53.3%** | 20.6% | +0.053 | 1.14 |
| 47 | AAVE | 2024-06-16 | 102 | 33.3% | -0.03% | **+3.7%** | 6.7% | **-14.0%** | 36.7% | +0.043 | 1.10 |
| 22 | BCH | 2024-06-16 | 98 | 35.7% | +0.12% | **+0.6%** | 14.5% | **+2.0%** | 45.6% | +0.013 | 1.03 |
| 42 | ONDO | 2024-06-16 | 95 | 43.2% | +0.00% | **-0.2%** | 13.4% | **-6.6%** | 51.3% | +0.001 | 1.00 |
| 30 | GRAM | 2024-06-14 | 85 | 30.6% | +0.25% | **-3.1%** | 15.6% | **+15.2%** | 40.3% | -0.032 | 0.92 |
| 17 | ADA | 2024-06-16 | 91 | 29.7% | +0.08% | **-4.9%** | 15.2% | **-5.1%** | 49.6% | -0.047 | 0.91 |
| 4 | BNB | 2024-06-16 | 104 | 34.6% | -0.12% | **-7.7%** | 14.7% | **-13.5%** | 23.9% | -0.073 | 0.82 |
| 51 | PUMP | 2025-07-10 | 54 | 31.5% | -0.99% | **-8.7%** | 11.6% | **-46.0%** | 55.6% | -0.165 | 0.64 |
| 25 | CC | 2025-10-31 | 36 | 19.4% | -0.82% | **-10.3%** | 13.1% | **-29.1%** | 43.0% | -0.298 | 0.42 |
| 14 | XMR | 2025-08-01 | 40 | 25.0% | -1.16% | **-10.7%** | 10.7% | **-38.2%** | 38.2% | -0.279 | 0.43 |
| 24 | LTC | 2024-06-16 | 122 | 38.5% | -0.38% | **-13.9%** | 18.1% | **-39.8%** | 46.1% | -0.119 | 0.71 |

Posisi yang masih terbuka di bar terakhir (tidak dihitung di atas): SOL L +0.83R, ZEC L +0.11R, XLM L -0.09R, GRAM L +0.96R, AAVE L +0.39R.

## Hyperliquid vs Binance perp — jendela waktu yang sama

Mesin dan parameter sama; yang beda cuma sumber candle. `Sinyal cocok` = persen sinyal Binance yang juga muncul di bar yang sama di Hyperliquid.

| Koin | Jendela | Sinyal cocok | Trx HL / BN | Exp R HL | Exp R BN | Total R HL | Total R BN |
|---|---|---|---|---|---|---|---|
| ETH | 2024-06-16 → 2026-08-31 | 78% | 93 / 93 | +0.231 | +0.317 | +21.5 | +29.5 |
| BNB | 2024-06-16 → 2026-08-31 | 62% | 99 / 101 | -0.085 | -0.037 | -8.4 | -3.7 |
| XRP | 2024-06-16 → 2026-08-31 | 86% | 84 / 82 | +0.409 | +0.288 | +34.3 | +23.6 |
| SOL | 2024-06-16 → 2026-08-31 | 85% | 90 / 91 | +0.215 | +0.239 | +19.3 | +21.8 |
| TRX | 2024-06-16 → 2026-08-31 | 60% | 105 / 92 | +0.077 | +0.230 | +8.1 | +21.1 |
| ZEC | 2025-04-19 → 2026-08-31 | 58% | 46 / 62 | +0.208 | +0.231 | +9.6 | +14.3 |
| DOGE | 2024-06-16 → 2026-08-31 | 83% | 96 / 85 | +0.427 | +0.460 | +41.0 | +39.1 |
| LINK | 2024-06-16 → 2026-08-31 | 77% | 100 / 91 | +0.152 | +0.154 | +15.2 | +14.0 |
| XMR | 2025-08-01 → 2026-08-31 | 38% | 37 / 59 | -0.225 | -0.147 | -8.3 | -8.7 |
| ADA | 2024-06-16 → 2026-08-31 | 77% | 88 / 84 | -0.051 | +0.138 | -4.5 | +11.6 |
| XLM | 2024-06-14 → 2026-08-31 | 49% | 58 / 81 | +0.192 | +0.091 | +11.1 | +7.3 |
| NEAR | 2024-06-16 → 2026-08-31 | 80% | 95 / 79 | +0.128 | +0.136 | +12.1 | +10.8 |
| BCH | 2024-06-16 → 2026-08-31 | 75% | 94 / 95 | -0.019 | -0.134 | -1.8 | -12.7 |
| UNI | 2024-06-16 → 2026-08-31 | 79% | 98 / 88 | +0.059 | +0.107 | +5.8 | +9.4 |
| LTC | 2024-06-16 → 2026-08-31 | 72% | 112 / 96 | -0.136 | -0.072 | -15.3 | -6.9 |
| SUI | 2024-06-16 → 2026-08-31 | 80% | 90 / 88 | +0.191 | +0.168 | +17.2 | +14.8 |
| AVAX | 2024-06-16 → 2026-08-31 | 78% | 89 / 79 | +0.132 | +0.130 | +11.8 | +10.3 |
| HBAR | 2024-06-16 → 2026-08-31 | 68% | 88 / 88 | +0.072 | +0.032 | +6.3 | +2.8 |
| SHIB | 2024-06-16 → 2026-08-31 | 78% | 81 / 89 | +0.227 | +0.160 | +18.4 | +14.3 |
Rata-rata sinyal cocok: **72%**. Total R di koin-koin ini: Hyperliquid **+193.3** vs Binance **+212.5**.


## Koin yang dilewati (urut market cap)

- #1 BTC (`bitcoin`) — btc
- #3 USDT (`tether`) — stablecoin
- #6 USDC (`usd-coin`) — stablecoin
- #10 FIGR_HELOC (`figure-heloc`) — tidak ada perp di Hyperliquid
- #15 WBT (`whitebit`) — tidak ada perp di Hyperliquid
- #16 USDS (`usds`) — stablecoin
- #18 RAIN (`rain`) — tidak ada perp di Hyperliquid
- #19 LEO (`leo-token`) — tidak ada perp di Hyperliquid
- #27 USDE (`ethena-usde`) — stablecoin
- #29 DAI (`dai`) — stablecoin
- #31 USD1 (`usd1-wlfi`) — stablecoin
- #35 CRO (`crypto-com-chain`) — tidak ada perp di Hyperliquid
- #36 USDG (`global-dollar`) — stablecoin
- #37 BTW (`bitway`) — tidak ada perp di Hyperliquid
- #38 PYUSD (`paypal-usd`) — stablecoin
- #39 M (`memecore`) — tidak ada perp di Hyperliquid
- #41 XAUT (`tether-gold`) — gold-backed
- #43 OKB (`okb`) — tidak ada perp di Hyperliquid
- #44 RLUSD (`ripple-usd`) — stablecoin
- #45 QNT (`quant-network`) — tidak ada perp di Hyperliquid
- #46 USYC (`hashnote-usyc`) — stablecoin
- #48 BUIDL (`blackrock-usd-institutional-digital-liquidity-fund`) — stablecoin
- #49 USDY (`ondo-us-dollar-yield`) — stablecoin

## File

- `summary.csv` — tabel per koin di atas
- `trades.csv` — semua transaksi (gross/biaya/funding/net dalam R)
- `portfolio_equity.csv` — kurva portofolio
- `universe.csv` — seleksi koin + alasan tiap yang dilewati
- `data/` — cache candle Hyperliquid mentah (hapus atau `--refresh` untuk unduh ulang)
