# Uji A — apakah memilih koin dari backtest menambah nilai?

Dibuat 2026-10-02 03:42 UTC oleh `tools/oos_selection.py`. Rencana & kriteria: `docs/OOS_PLAN.md` §3, commit `74770a1 2026-10-02T10:37:52+07:00`.

Universe 24 koin (hidup di Hyperliquid sejak Jun 2024). Seleksi = 12 teratas menurut profit @risiko 1% di jendela seleksi. Data = cache backtest Hyperliquid (tidak diunduh ulang). Biaya: fee 0.045% + slippage 0.01% per sisi + funding per jam asli.

**Paritas dengan backtest asli:** ✅ jumlah transaksi dan total R ke-24 koin sama persis dengan `backtest/hyperliquid/summary.csv`.

## A1 — walk-forward (uji utama): **TIDAK JELAS**

Transaksi OOS dari 5 jendela uji disambung. Kriteria: selisih top − bottom > 0 dan t Welch ≥ 2.0 → LULUS; > 0 tapi t < 2.0 → TIDAK JELAS; ≤ 0 → GAGAL.

| Kelompok | Trx | Exp R | Win | PF | t |
|---|---|---|---|---|---|
| Top 12 (dipilih) | 671 | +0.136 | 38.9% | 1.36 | +2.67 |
| Bottom 12 | 678 | +0.100 | 39.4% | 1.26 | +2.04 |
| Semua 24 | 1349 | +0.118 | 39.1% | 1.31 | +3.34 |

**Selisih top − bottom: +0.035 R/trx, t Welch +0.50** (t dari selisih rata-rata per bulan: +1.05, 16 bulan).

### Per jendela

| Jendela uji | Exp top (IS) | Exp top (OOS) | Exp bottom (OOS) | Selisih | t | Spearman | Top 12 |
|---|---|---|---|---|---|---|---|
| 2025-06-16 → 2025-09-16 | +0.270 | +0.392 (176) | +0.229 (154) | +0.163 | +0.94 | +0.20 | DOGE SHIB LINK SUI AVAX XRP WLD SOL ENA ETH TRX MNT |
| 2025-09-16 → 2025-12-16 | +0.332 | -0.039 (81) | -0.063 (95) | +0.024 | +0.16 | -0.11 | DOGE WLD AVAX XRP ENA SUI SHIB ETH MNT LINK XLM TRX |
| 2025-12-16 → 2026-03-16 | +0.288 | -0.001 (102) | -0.004 (97) | +0.003 | +0.02 | +0.07 | ETH WLD DOGE ENA XLM SUI HBAR NEAR AVAX XRP SOL SHIB |
| 2026-03-16 → 2026-06-16 | +0.301 | -0.126 (150) | -0.082 (144) | -0.044 | -0.35 | -0.05 | ETH WLD DOGE ENA SUI HBAR XLM DOT MNT XRP NEAR SHIB |
| 2026-06-16 → akhir data | +0.211 | +0.274 (162) | +0.272 (188) | +0.002 | +0.02 | -0.07 | XLM XRP NEAR DOGE MNT ETH DOT SUI TAO ONDO HBAR WLD |

Top > bottom di **4/5** jendela. Spearman rata-rata **+0.01** (ranking seleksi vs expectancy uji per koin). Expectancy top di jendela seleksi rata-rata +0.280 R → di jendela uji +0.136 R.

## A2 — belah dua (deskriptif)

| Arah | Exp top | Exp bottom | Selisih | t | Spearman | Top 12 |
|---|---|---|---|---|---|---|
| H1 → H2 | +0.073 (585) | +0.074 (590) | -0.001 | -0.01 | +0.05 | DOGE XRP LINK SHIB AVAX ENA HBAR SUI MNT WLD TRX ETH |
| H2 → H1 | +0.154 (571) | +0.186 (565) | -0.032 | -0.41 | +0.07 | DOGE TAO XRP DOT ETH ONDO SOL MNT NEAR SUI BCH UNI |

H2 → H1 memakai data masa depan untuk memilih; ia hanya mengukur apakah ranking koin bertahan, bukan apakah ranking bisa memprediksi.

## Seberapa sering tiap koin masuk top (5 jendela A1)

| Koin | Kelompok live | Masuk top | Rata-rata ranking |
|---|---|---|---|
| DOGE | L | 5/5 | 2.4 |
| WLD | O | 5/5 | 5.0 |
| ETH | L | 5/5 | 5.2 |
| SUI | L | 5/5 | 5.8 |
| XRP | L | 5/5 | 6.4 |
| ENA | L | 4/5 | 7.0 |
| XLM | O | 4/5 | 8.8 |
| AVAX | O | 3/5 | 9.8 |
| MNT | L | 4/5 | 9.8 |
| SHIB | L | 4/5 | 10.2 |
| HBAR | O | 3/5 | 10.8 |
| NEAR | L | 3/5 | 11.4 |
| DOT | L | 2/5 | 12.4 |
| LINK | L | 2/5 | 12.8 |
| SOL | L | 2/5 | 12.8 |
| TAO | L | 1/5 | 13.0 |
| TRX | O | 2/5 | 15.6 |
| UNI | O | 0/5 | 16.8 |
| AAVE | O | 0/5 | 17.6 |
| ADA | O | 0/5 | 20.0 |
| ONDO | O | 1/5 | 21.0 |
| LTC | O | 0/5 | 21.2 |
| BNB | O | 0/5 | 21.4 |
| BCH | O | 0/5 | 22.8 |

## File

- `windows.csv` — tabel per jendela (A1 + A2)
- `rankings.csv` — ranking 24 koin di tiap jendela seleksi
- `oos_trades.csv` — transaksi OOS A1 dengan jendela dan kelompoknya
