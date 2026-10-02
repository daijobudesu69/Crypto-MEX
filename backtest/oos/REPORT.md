# Hasil uji out-of-sample MEX 3.0 — 2 Okt 2026

Rencana dan kriteria dikunci di `docs/OOS_PLAN.md` (commit `74770a1`), script dan
tes di-commit sebelum dijalankan (`38e8322`). Setiap uji dijalankan **satu kali**.
Rincian: [`selection/REPORT.md`](selection/REPORT.md) dan
[`pre2023/REPORT.md`](pre2023/REPORT.md).

Universe: 24 koin yang hidup di Hyperliquid tanpa putus sejak Jun 2024 (keputusan
pemilik). 12 dari 13 koin live ikut (HYPE keluar).

## Vonis

| Uji | Pertanyaan | Angka kunci | Vonis |
|---|---|---|---|
| **A1** | Apakah memilih koin dari backtest menambah nilai? (walk-forward, 5 kuartal) | top **+0,136** vs bottom **+0,100** R/trx · selisih **+0,035**, t **0,50** · Spearman rata-rata **+0,01** | **TIDAK JELAS** |
| **B1** | Apakah strategi punya edge di data yang belum pernah dilihat? (Binance, Jan 2020 → Mei 2023, 18 koin) | **+0,001 R/trx**, t **0,06**, PF 1,00 · koin positif **5/18** | **TIDAK JELAS** (batas GAGAL adalah ≤ 0) |
| **B2** | Apakah kelompok koin live lebih bagus di periode itu? | L **+0,004** vs O **−0,001** · t **0,10** | **TIDAK JELAS** |

## Apa artinya

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
*semua* koin, termasuk top). Edge MEX muncul dalam episode. Periode panjang tanpa
edge (±9 bulan di data Hyperliquid, ±3 tahun di 2021–23) adalah bagian normal dari
strategi ini, bukan pengecualian.

**3. Koin live tidak istimewa di masa lalu.** Dari 8 koin live yang punya data
2020–23, hanya ETH (+0,196) dan DOGE (+0,200) yang positif. XRP, SOL, LINK, NEAR,
DOT, SHIB negatif. Koin "buruk" di Hyperliquid seperti ADA justru terbaik
(+0,228). Korelasi ranking antar periode: +0,15.

## Konsekuensi (sesuai tabel §6 rencana)

| Hasil | Konsekuensi yang sudah ditetapkan |
|---|---|
| A tidak jelas | **Patokan live = skenario B atau C (+0,105 R atau lebih rendah), bukan A.** Universe **tidak** perlu diganti: menukar koin juga hanya noise. |
| B1 tidak jelas | "Edge ada tapi lemah; perkuat kehati-hatian, forward test tetap penentu." |

**Catatan jujur tentang B1:** secara aturan vonisnya TIDAK JELAS, tapi angkanya
hanya **0,001 R** di atas garis GAGAL. Dari data yang belum pernah dilihat,
ekspektasi realistis edge setelah biaya adalah **antara skenario C (+0,046 R,
+12%/thn, DD 50%) dan D (0 R, −12%/thn, DD 58%)** di project log §5.11, bukan B.

Parameter strategi **tidak diubah**. Hasil ini juga **bukan** alasan untuk
mengoptimasi: T2 sudah menunjukkan bahwa re-optimasi merusak.

## Opsi untuk pemilik (belum ada yang dijalankan)

1. **Turunkan ekspektasi tertulis** di project log dan circuit breaker ke skenario
   C–D. Breaker 40% dipasang di atas DD skenario B (39%); di C–D, DD 50–58% adalah
   hal yang wajar, jadi breaker akan menyala pada perilaku "normal".
2. **Risiko per transaksi lebih kecil** (mis. 0,5%) sampai forward test punya
   ≥ 100 transaksi.
3. **Pra-registrasi aturan berhenti untuk forward/live sekarang**, sebelum hasilnya
   terlihat. Contoh: setelah 100 transaksi, kalau expectancy ≤ 0, mode kembali ke
   `dry`.
4. **Tetap di `dry` lebih lama.** Satu-satunya bukti yang tersisa adalah forward
   test (Uji C), dan bukti itu belum ada.

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
- **Batasan yang tetap berlaku:** Binance ≠ Hyperliquid, tapi head-to-head §5.7
  menunjukkan edge yang sama di bar identik. t per transaksi melebih-lebihkan
  bukti karena koin bergerak bersama; t per bulan dilaporkan di sampingnya, dan
  hasilnya lebih kecil lagi.
