# Rencana uji out-of-sample (OOS) MEX 3.0 — pra-registrasi

Ditulis 2026-10-02, **sebelum** satu pun uji di bawah dijalankan. Dokumen ini
di-commit lebih dulu supaya kriteria lulus tidak bisa disesuaikan setelah hasil
terlihat. Laporan hasil (`backtest/oos/REPORT.md`) wajib mengutip hash commit
dokumen ini dan mencatat setiap penyimpangan dari rencana.

## 1. Kenapa uji ini perlu

Keputusan live bertumpu pada dua klaim yang berbeda:

1. **Strategi (parameter beku) punya edge.** Sudah diuji berkali-kali, tapi semua
   data sejak Jun 2023 sudah dipakai (MEX 1.0, MEX 2.0, backtest Hyperliquid), dan
   OOS sebelumnya selalu meluruh (21% di ETH, 40% di 20 koin).
2. **13 koin pilihan lebih bagus dari rata-rata** (+0,221 R vs +0,105 R). Belum
   pernah diuji. Klaim inilah yang membedakan skenario A (+181%/thn) dari skenario
   B (+53%/thn) di project log §5.11.

| Periode | Sudah dipakai untuk | Status |
|---|---|---|
| Jan 2024 → Ags 2026 | Asal parameter (TradingView ETH) | in-sample |
| Jun 2023 → Ags 2026 | MEX 1.0 (ETH), MEX 2.0 (20 koin) | in-sample |
| Jun 2024 → Sep 2026 | Backtest HL 30 koin + pemilihan 13 koin | in-sample |
| **Jan 2020 → Mei 2023** | Belum pernah disentuh | **OOS asli** |
| Setelah 26 Sep 2026 | Forward test | OOS asli, masih kecil |

## 2. Aturan yang berlaku untuk semua uji

- `mex/strategy.py` dan `config.yaml → strategy` **tidak diubah**. Mesin =
  `run_backtest()` dari `tools/backtest_hyperliquid.py` (memanggil `step()` yang
  sama dengan forward test).
- Tidak ada optimasi parameter apa pun, termasuk walk-forward re-optimization.
- Setiap uji dijalankan **sekali** dengan spesifikasi ini. Kalau ada bug yang harus
  diperbaiki lalu dijalankan ulang, bug dan perbaikannya dicatat di laporan.
- Hasil buruk dilaporkan apa adanya. Hasil tidak dipakai untuk mengganti koin live
  tanpa keputusan pemilik.

### Universe (keputusan pemilik 2026-10-02)

Hanya koin yang **hidup terus-menerus di Hyperliquid dari Jun 2024 sampai
sekarang**: candle mulai ≤ 2024-06-16, tanpa celah, dan tidak berstatus delisted
di `meta` Hyperliquid per 2026-10-02. Dari 30 koin backtest, **24 lolos**:

| Kelompok | Koin |
|---|---|
| **L** — koin live yang lolos (12) | ETH, XRP, SOL, DOGE, TAO, MNT, SUI, SHIB (kSHIB), DOT, ENA, LINK, NEAR |
| **O** — koin lain yang lolos (12) | BNB, TRX, ADA, XLM, BCH, UNI, LTC, AVAX, HBAR, AAVE, ONDO, WLD |
| Dikeluarkan (6) | HYPE (listing 2024-12), ZEC (2025-04), XMR (2025-08), PUMP (2025-07), CC (2025-10), GRAM/TON (delisting 2026-06 → relisting) |

## 3. Uji A — apakah *prosedur* memilih koin menambah nilai?

**Data:** cache candle + funding Hyperliquid yang sama dengan backtest
(`backtest/hyperliquid/data/`, sampai bar 2026-09-27 08:00), tanpa `--refresh`.
Biaya: fee 0,045% + slippage 0,01% per sisi + funding per jam asli.

**Cara kerja:** strategi dijalankan sekali per koin di seluruh deret (indikator
kontinu), lalu transaksi dibagi ke jendela:

- **Jendela seleksi:** transaksi yang **masuk dan keluar** di dalam jendela.
- **Jendela uji:** transaksi yang **masuk** di dalam jendela (boleh keluar sesudahnya).
  Posisi yang belum tertutup di akhir data dibuang.

**Aturan seleksi** (sama dengan 28 Sep): ranking 24 koin menurut profit @risiko 1%
(dimajemukkan, `return_pct`) di jendela seleksi, ambil **12 teratas** = "top",
12 sisanya = "bottom".

**A1 — walk-forward (UJI UTAMA).** Jendela uji per kuartal yang tidak tumpang
tindih; seleksi = 12 bulan tepat sebelum awal jendela uji:

| # | Jendela seleksi | Jendela uji |
|---|---|---|
| 1 | 2024-06-16 → 2025-06-16 | 2025-06-16 → 2025-09-16 |
| 2 | 2024-09-16 → 2025-09-16 | 2025-09-16 → 2025-12-16 |
| 3 | 2024-12-16 → 2025-12-16 | 2025-12-16 → 2026-03-16 |
| 4 | 2025-03-16 → 2026-03-16 | 2026-03-16 → 2026-06-16 |
| 5 | 2025-06-16 → 2026-06-16 | 2026-06-16 → akhir data |

Transaksi OOS kelima jendela disambung menjadi satu sampel "top" dan satu "bottom".

**A2 — belah dua (pendukung).** H1 = 2024-06-14 → 2025-08-01, H2 = 2025-08-01 →
akhir data. Seleksi di H1 → uji di H2, dan sebaliknya (H2 → H1, hanya mengukur
persistensi ranking, bukan prediksi ke depan).

**Metrik:** expectancy net (R/trx), jumlah trx, win rate, PF, t-stat untuk top,
bottom, dan semua 24; selisih top − bottom dengan uji t Welch di level transaksi;
juga t dari jumlah R per bulan (karena transaksi antar koin berkorelasi); korelasi
Spearman ranking seleksi vs expectancy uji per jendela; jumlah jendela top > bottom.

**Kriteria (berlaku untuk A1):**

| Hasil | Syarat |
|---|---|
| **Lulus** — seleksi menambah nilai | selisih top − bottom > 0 **dan** t Welch ≥ 2,0 |
| **Tidak jelas** | selisih > 0, t < 2,0 |
| **Gagal** — seleksi tidak menambah nilai | selisih ≤ 0 |

A2, Spearman, dan hitungan jendela bersifat deskriptif, tidak mengubah vonis A1.

## 4. Uji B — OOS waktu di Binance perp, Jan 2020 → Mei 2023

**Data:** arsip publik `data.binance.vision` (USDT-M perp), candle 4H dan funding,
2020-01 s/d 2023-06 (Juni hanya untuk menutup posisi yang masuk akhir Mei). SHIB =
`1000SHIBUSDT`. Koin yang tidak punya perp Binance di periode ini, atau punya
< 300 bar di jendela, dicatat sebagai "tidak ada data", **tidak diganti**.
Perkiraan yang tersedia: L = ETH, XRP, SOL, DOGE, SHIB, DOT, LINK, NEAR; O = BNB,
TRX, ADA, XLM, BCH, UNI, LTC, AVAX, HBAR, AAVE (daftar final ditentukan oleh data).

**Biaya:** fee taker Binance 0,05% + slippage 0,01% per sisi (sama dengan
`tools/h2h_hl_binance.py`) + funding 8 jam asli dari arsip. Cakupan funding
dilaporkan per koin.

**Jendela:** transaksi yang **masuk** 2020-01-01 → 2023-05-31. Indikator dihitung
dari awal data yang ada; tidak ada data pemanasan sebelum Jan 2020.

**Mengapa Binance boleh:** head-to-head bar identik (§5.7 project log) menunjukkan
venue mengubah *transaksi mana* yang didapat, bukan besar edge-nya (+0,298 vs
+0,301 R).

**B1 — edge strategi di periode yang belum pernah dilihat (semua koin yang ada data):**

| Hasil | Syarat |
|---|---|
| **Lulus** | expectancy gabungan > 0 **dan** t ≥ 2,0 **dan** ≥ 60% koin expectancy > 0 |
| **Tidak jelas** | expectancy > 0 tapi salah satu syarat lain tidak terpenuhi |
| **Gagal** | expectancy gabungan ≤ 0 |

**B2 — apakah kelompok L lebih bagus dari O di periode lain:** kriteria sama
dengan tabel Uji A (selisih L − O, t Welch).

**Deskriptif:** per tahun (2020, 2021, 2022, 2023), long vs fade short, t dari R
bulanan, korelasi Spearman expectancy per koin Binance 2020–23 vs Hyperliquid
2024–26.

## 5. Uji C — forward test

Sudah berjalan. Dinilai setelah ≥ 100 transaksi engine `mex-fwd-2.2.0`
terhadap **+0,105 R** (project log §14). Tidak ada kode baru.

## 6. Apa artinya untuk keputusan (ditetapkan sekarang)

| Hasil | Arti |
|---|---|
| A lulus | Premi seleksi nyata; patokan live boleh di antara skenario A dan B |
| A tidak jelas / gagal | Patokan live = skenario B atau C (+0,105 R atau lebih rendah), bukan A. Universe **tidak** perlu diganti: ranking tidak punya daya prediksi, jadi menukar koin juga noise |
| B1 lulus | Edge strategi bertahan di regime yang belum pernah dilihat (bull 2021, bear 2022) |
| B1 tidak jelas | Edge ada tapi lemah; perkuat kehati-hatian, forward test tetap penentu |
| B1 gagal | **Red flag**: bahas sebelum mode `live` |
| B2 | Menguatkan atau melemahkan vonis A; tidak berdiri sendiri |

Tidak ada hasil yang mengubah parameter strategi.

## 7. Tebakan sebelum menjalankan

Dicatat supaya bisa dicek: premi seleksi di Uji A kemungkinan besar **menyusut
banyak** (vonis "tidak jelas" atau "gagal"), karena galat baku per koin ±0,13 R
sehingga ranking bagian tengah sebagian besar noise (project log §5.8). Untuk B1,
tebakan: expectancy positif tapi di bawah +0,105 R.

## 8. Hasil kerja

- `tools/oos_selection.py` (Uji A) dan `tools/oos_binance_pre2023.py` (Uji B), plus tes
- `backtest/oos/selection/`, `backtest/oos/pre2023/` (CSV + laporan per uji)
- `backtest/oos/REPORT.md` — vonis per kriteria di atas
- Data mentah Binance di `backtest/oos/pre2023/data/` (tidak di-commit)
