# Hasil uji out-of-sample MEX 3.0 — 2 Okt 2026

Rencana dan kriteria dikunci di `docs/OOS_PLAN.md` (commit `74770a1`), script dan
tes di-commit sebelum dijalankan (`38e8322`). Setiap uji dijalankan **satu kali**.
Rincian: [`selection/REPORT.md`](selection/REPORT.md) dan
[`pre2023/REPORT.md`](pre2023/REPORT.md).

Universe: 24 koin yang hidup di Hyperliquid tanpa putus sejak Jun 2024 (keputusan
pemilik). 12 dari 13 koin live ikut (HYPE keluar).

§1–§3 adalah uji yang dipra-registrasi. §4–§6 adalah **analisis lanjutan yang
dibuat setelah melihat hasil** (atas pertanyaan pemilik). Analisis lanjutan itu
menjelaskan masa lalu, tapi tidak membuktikan aturan apa pun. §7 berisi keputusan
yang diambil dari semuanya.

## 1. Vonis (pra-registrasi)

| Uji | Pertanyaan | Angka kunci | Vonis |
|---|---|---|---|
| **A1** | Apakah memilih koin dari backtest menambah nilai? (walk-forward, 5 kuartal) | top **+0,136** vs bottom **+0,100** R/trx · selisih **+0,035**, t **0,50** · Spearman rata-rata **+0,01** | **TIDAK JELAS** |
| **B1** | Apakah strategi punya edge di data yang belum pernah dilihat? (Binance, Jan 2020 → Mei 2023, 18 koin) | **+0,001 R/trx**, t **0,06**, PF 1,00 · koin positif **5/18** | **TIDAK JELAS** (batas GAGAL adalah ≤ 0) |
| **B2** | Apakah kelompok koin live lebih bagus di periode itu? | L **+0,004** vs O **−0,001** · t **0,10** | **TIDAK JELAS** |

## 2. Apa artinya

**1. Memilih koin dari backtest tidak terbukti menambah apa pun.** Di kelima
jendela, koin top 12 memang punya expectancy rata-rata **+0,280 R** di jendela
seleksinya, tapi di jendela berikutnya hanya **+0,136 R**. Angka itu hampir sama
dengan koin bottom (+0,100 R). Ranking koin tidak memprediksi apa-apa (Spearman
≈ 0), dan uji belah dua memberi selisih **−0,001** dan **−0,032**. Ini sesuai
dengan galat per koin ±0,13 R di project log §5.8. Jadi **+0,221 R untuk 13 koin
pilihan adalah bias seleksi**, bukan edge tambahan.

**2. Di satu-satunya periode yang belum pernah disentuh, edge setelah biaya ≈ 0.**
Sebelum biaya +0,041 R; fee + slippage memakan 0,030 R dan funding 0,010 R.
Hasilnya sangat bergantung pada tahun:

| Tahun entry | Trx | Exp R | t |
|---|---|---|---|
| 2020 | 535 | **+0,120** | +2,19 |
| 2021 | 844 | −0,003 | −0,06 |
| 2022 | 595 | −0,032 | −0,81 |
| 2023 (Jan–Mei) | 370 | **−0,107** | −2,10 |

Ini konsisten dengan pola lama: **T1 OOS MEX 1.0 (21%), MEX 2.0 (40%), dan Uji A
jendela Sep 2025 → Jun 2026** (tiga kuartal berturut-turut ≈ 0 atau negatif untuk
*semua* koin, termasuk top). Edge MEX muncul dalam episode.

**3. Koin live tidak istimewa di masa lalu.** Dari 8 koin live yang punya data
2020–23, hanya ETH (+0,196) dan DOGE (+0,200) yang positif. XRP, SOL, LINK, NEAR,
DOT, SHIB negatif. Koin "buruk" di Hyperliquid seperti ADA justru terbaik
(+0,228). Korelasi ranking antar periode: +0,15.

## 3. Konsekuensi yang sudah ditetapkan (§6 rencana)

| Hasil | Konsekuensi |
|---|---|
| A tidak jelas | Patokan live bukan skenario A. Universe **tidak perlu** diganti karena menukar koin juga noise. (Pemilik tetap merevisi universe, lihat §7.) |
| B1 tidak jelas | "Edge ada tapi lemah; perkuat kehati-hatian, forward test tetap penentu." |

Parameter strategi **tidak diubah**. Hasil ini juga **bukan** alasan untuk
mengoptimasi: T2 sudah menunjukkan bahwa re-optimasi merusak.

## 4. Regime pasar: venue dan koin yang sama, 2020 → 2026

**Kenapa ditambahkan:** pemilik bertanya apakah 2020–23 (era retail) mewakili
2024+ (era ETF/institusi). Untuk menjawabnya, dipakai Binance perp dengan 16 koin
yang sama, deret 4H tanpa putus dari Jan 2020 sampai Ags 2026. Script:
`tools/regime_diagnostic.py`, hasil di `regime/`.

| Era | Trx | Exp R | t | Lanjutan breakout 5 bar (ATR) | Breakout gagal | Kuartal positif |
|---|---|---|---|---|---|---|
| 2020–21 (bull retail) | 1.258 | +0,058 | 1,65 | +0,32 | 56% | 4/8 |
| 2022–23 (bear, pra-ETF) | 1.365 | **−0,072** | −2,60 | −0,07 | 62% | 3/8 |
| 2024–26 (pasca-ETF) | 1.748 | **+0,117** | 3,96 | +0,04 | 58% | 7/10 |
| **Seluruh 2020–26** | 4.371 | **+0,041** | 2,30 | | | |

- **Yang menjelaskan hasil per kuartal adalah satu perilaku pasar: apakah breakout
  berlanjut.** Korelasinya dengan lanjutan breakout +0,69 dan dengan porsi
  breakout gagal −0,65. Efisiensi tren (+0,24), volatilitas (+0,19), dan
  autokorelasi (−0,05) hampir tidak berpengaruh.
- **Garis pemisahnya bukan retail vs institusi.** 2020–21 (retail) juga positif,
  dan di era ETF ada tiga kuartal negatif berturut-turut (2025Q4 −0,18, 2026Q1
  −0,04, 2026Q2 −0,13). Sebagian dari +0,117 di 2024–26 adalah in-sample karena
  parameter berasal dari periode itu.
- **Regime tidak bisa diketahui lebih dulu.** Korelasi expectancy satu kuartal
  dengan kuartal berikutnya +0,23 (tidak signifikan, 26 kuartal), dan di level
  koin-kuartal 0,00.
- **Koreksi:** patokan tengah yang adil adalah rata-rata semua regime, **±+0,04
  R/trx**, hampir persis **skenario C** project log (+0,046 R, ±+12%/thn). Ini
  bukan "antara C dan D" seperti yang tertulis di versi pertama laporan ini.

## 5. Kalau tidak trade saat bear market

Script `tools/bear_filter_study.py`. Tiap varian diputar ulang bar demi bar dengan
sinyal diblok (koin bebas mengambil sinyal berikutnya). R dijumlahkan, 1 R ≈ 1%
modal awal.

| Varian | Trx | Exp R | Total R | DD maks (R) | Rugi 12 bln terburuk |
|---|---|---|---|---|---|
| V0 semua sinyal | 4.371 | +0,041 | +179 | 148 | −109 |
| V1 hindsight: skip 2022–23 (mustahil di dunia nyata) | 3.006 | +0,092 | +277 | 74 | −44 |
| V2 BTC > SMA200 harian (real-time) | 2.886 | +0,057 | +164 | 102 | −81 |
| V3 sama, fade short tetap jalan | 3.190 | +0,057 | +182 | 99 | −85 |

- **Musuh MEX bukan bear market.** Di 2022 (BTC −65%) MEX hanya −9,9 R: long
  −28,5 R, tapi fade short **+18,6 R**. Tahun terburuk adalah **2023 (BTC
  +154%)** dengan long **−84 R**, karena pasar naik dengan breakout yang sering
  gagal dan volatilitas terendah.
- **Aturan real-time hanya memangkas drawdown sekitar sepertiga dan tidak menambah
  total.** Di sebagian besar 2023 BTC berada di atas SMA200, dan aturan ini ikut
  memblok transaksi bagus di 2020 dan 2024.

## 6. Akun $100 dengan aturan live

Script `tools/account_sim.py` (hasil di `regime/account_sim.csv`). Risiko 1% saldo
dimajemukkan, minimum $10 (dilewati kalau > 2× target risiko), isolated 4x dengan
margin 95% dari saldo bebas. Data: 16 koin Binance perp, sampai Ags 2026.

**Waktu mulai menentukan segalanya:**

| Mulai | Ikut terus | DD maks | Breaker 40% tanpa reset |
|---|---|---|---|
| Jan 2020 | **$289** (terendah $52) | 82% | $176 (berhenti Mar 2022) |
| Jan 2021 | $145 | 83% | $110 |
| Nov 2021 | $128 | 77% | $66 |
| Jan 2022 | $136 | 77% | $68 |
| Jan 2023 | $157 | 74% | $76 |
| Jan 2024 | **$487** | 53% | $348 |
| Jan 2025 | $171 | 53% | $122 |
| Jan 2026 | $101 | 46% | $61 |

Dari puncak 2021 ke dasar Des 2023, saldo turun ±80%. Saldo baru kembali ke puncak
2021 pada Jul 2025.

**Breaker sebagai penanda bear, lalu masuk lagi mengikuti siklus 4 tahun**
(dibuat setelah melihat data; teori siklusnya sudah ada sebelumnya, tapi sampelnya
hanya 2 siklus):

| Aturan (mulai Jan 2020, $100) | Akhir | DD maks | Catatan |
|---|---|---|---|
| S0 ikut terus | $289 | 82% | |
| S1 breaker 40%, berhenti selamanya | $176 | 41% | berhenti Mar 2022 |
| S2 breaker 40%, masuk lagi 1 Jan tahun halving | **$612** | 41% | berhenti Mar 2022, mulai Jan 2024, **berhenti lagi Apr 2026** |
| S2′ masuk lagi pada tanggal halving | $534 | 41% | |
| S4 kalender saja: trade 2020–21 & 2024–25 | $860 | 40% | |
| Breaker **30%** + masuk 1 Jan 2024 (risiko 1%) | $793 | 33% | berhenti Jul 2021, berhenti lagi Apr 2026 |

**Risiko 0,5% vs 1%:** ikut terus $179 (DD 57%) vs $289 (DD 82%). Breaker harus
ikut diturunkan. Dengan risiko 0,5%, breaker 40% baru menyala Jul 2023 di $96,
lebih telat dan lebih buruk dari versi 1%. Di akun kecil, minimum $10 membuat
risiko 0,5% praktis naik ke 0,5–1% untuk stop yang lebar.

**Konsekuensi aturan siklus untuk sekarang:** S2 dan S4 sama-sama berkata
2026–2027 adalah fase "jangan trade" (breaker simulasi menyala 23 Apr 2026).

## 7. Keputusan pemilik, 2 Okt 2026

| Keputusan | Status |
|---|---|
| Circuit breaker 40% → **30%** | ✅ di `main` ([PR #15](https://github.com/daijobudesu69/Crypto-MEX/pull/15)) |
| Universe 13 → **10 koin**: ETH, DOGE, XRP, SOL, HYPE, TAO, MNT, SUI, ENA, **XLM** (NEAR, DOT, LINK, SHIB keluar) — sebagai bagian dari OOS | [PR #16](https://github.com/daijobudesu69/Crypto-MEX/pull/16), `mex-fwd-2.3.0` |
| Review forward test di **30, 50, 100 transaksi**, kriteria MERAH/KUNING/HIJAU dikunci sebelum ada transaksi | `CHANGELOG.md` 2026-10-02 (PR #16) |
| Risiko per transaksi | tetap **1%** (0,5% dibahas, belum diputuskan) |
| Strategi & parameter | **tidak diubah** |

Uji C (forward test) adalah satu-satunya bukti yang benar-benar baru. Sebaran per
transaksi lebar (sd 1,35 R, median −0,16 R), jadi bahkan di 100 transaksi skenario
C dan edge nol hampir tidak bisa dibedakan. Review hanya bisa menangkap "jauh
lebih buruk dari C" atau "jelas positif".

## Cek tebakan sebelum menjalankan (§7 rencana)

| Tebakan | Hasil |
|---|---|
| A: premi seleksi menyusut banyak, vonis TIDAK JELAS/GAGAL | ✅ tepat: +0,221 → selisih +0,035 |
| B1: positif tapi di bawah +0,105 R | ⚠️ lebih buruk dari tebakan: +0,001 R |

## Kualitas data dan penyimpangan

- **Paritas Uji A:** jumlah transaksi dan total R ke-24 koin sama persis dengan
  `backtest/hyperliquid/summary.csv`.
- **Uji B:** funding tercakup 100% di setiap bulan yang punya candle. 0 posisi
  masih terbuka di akhir data. Arsip Binance punya dua celah (25 Feb → 1 Mar dan
  31 Mar → 3 Apr 2022) di 7 koin. 45 transaksi di sekitar celah itu tidak mengubah
  hasil (+0,0015 → +0,0017 R).
- **Koin tanpa data 2020–23:** TAO, MNT, ENA, ONDO, WLD (belum ada perp Binance),
  SUI (hanya 170 bar). Tidak diganti.
- **Sebelum uji dijalankan,** tes menemukan bug di `oos_selection._ts`
  (timestamp ber-timezone) dan satu angka harapan yang salah di tes. Keduanya
  diperbaiki sebelum commit `38e8322`. **Tidak ada uji yang dijalankan ulang.**
- **§4–§6:** deret 2020–26 menyambung arsip yang diunduh (2020-01 → 2023-06)
  dengan arsip lokal MEX 2.0 (2023-06 → 2026-08); V0 cocok persis dengan
  diagnostik regime (4.371 trx, +179,4 R). DOT dan AAVE tidak punya arsip lokal
  setelah 2023 dan tidak ikut. BTC harian = Binance **spot** (arsip perp baru
  mulai 2020-01, terlalu pendek untuk SMA200). Angka akun $100 di percakapan
  berbeda beberapa dolar dari §6 karena versi awalnya belum memakai sisa margin
  95%; §6 yang berlaku.
- **Batasan yang tetap berlaku:** Binance ≠ Hyperliquid, tapi head-to-head §5.7
  menunjukkan edge yang sama di bar identik. t per transaksi melebih-lebihkan
  bukti karena koin bergerak bersama; t per bulan dilaporkan di sampingnya, dan
  hasilnya lebih kecil lagi.

## File

- `selection/`, `pre2023/` — uji pra-registrasi (A, B)
- `regime/quarters.csv`, `coin_quarters.csv`, `eras.csv`, `correlations.json` — §4
- `regime/bear_filter_summary.csv`, `bear_filter_yearly.csv`, `btc_below_sma200_runs.csv`, `trades_V0..V3.csv` — §5
- `regime/account_sim.csv` — §6
- Script: `tools/oos_selection.py`, `tools/oos_binance_pre2023.py`, `tools/regime_diagnostic.py`, `tools/bear_filter_study.py`, `tools/account_sim.py`
