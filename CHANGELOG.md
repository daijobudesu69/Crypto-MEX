# Changelog

Setiap perubahan pada `config.yaml` atau aturan strategi WAJIB dicatat di sini
dengan tanggal dan alasan. Forward test yang parameternya diubah diam-diam di
tengah jalan tidak membuktikan apa pun.

## 2026-08-31 — logger Sheets lewat service account

- `mex/sheets.py`: menulis ke Sheets API langsung memakai
  `GOOGLE_SERVICE_ACCOUNT_JSON` + `GSHEET_SPREADSHEET_ID`. Tab `events`,
  `trades`, `runs` dibuat otomatis beserta headernya. Apps Script webhook tetap
  didukung sebagai alternatif.
- `sheet_ok` sekarang melaporkan hasil pengiriman sebenarnya
  (`ok` / `partial_n/m` / `failed` / `unreachable` / `not_configured`),
  bukan lagi sekadar apakah secret terisi.

**Insiden keamanan (sudah ditangani).** Nilai secret yang salah bentuk pernah
ikut tercetak ke log Actions repo publik lewat teks exception `requests`; masking
GitHub tidak menutupinya karena nilainya multi-baris. Log yang terdampak sudah
dihapus dan kunci sudah dirotasi. Kode sekarang memvalidasi bentuk nilai sebelum
dipakai dan melaporkan exception berdasarkan tipenya saja — tidak pernah pesannya.

## 2026-08-30 — mulai forward test

Konfigurasi awal, identik dengan baseline yang dibacktest (spec §2):

- `n_lookback=20 · vol_mult=1.5 · ema 20/50 · rsi_len=14 · roc_len=5`
- `rsi_confirm=55 · atr_len=14 · atr_sl_mult=1.5 · allow_shorts=true`

Keputusan yang diambil dari sesi analisis dan diterapkan di sini:

| Keputusan | Alasan |
|---|---|
| Exit Mode B (stop order di bursa), tanpa TP | 118/118 transaksi backtest keluar lewat trailing stop; PnL/DD 3,72 vs 1,04 untuk bracket TP+SL statis |
| Callback rate dibekukan di entry = `1,5×ATR/harga` | +14,24%/thn, DD 8,34%, PnL/DD 3,55 — setara versi yang butuh geser manual tiap 4 jam (3,72), tapi cukup satu order |
| Callback dihitung ulang **tiap transaksi**, bukan angka tetap | angka tetap 2,75% (rata-rata) memangkas return jadi +7,62%/thn dan jadi **rugi** di periode 2026 |
| Tanpa take profit | TP 4R hanya menambah 0,6 pp/thn dan kena di 4% transaksi; TP 1R justru memangkas profit sepertiga |
| Fade-short tetap aktif | sesuai spec; T8-F6 menunjukkan mematikannya sedikit memperburuk hasil |
| Zona entry ±0,5R, hangus 8 jam | cron GitHub Actions bisa telat sampai 6 jam |
| Ukuran posisi tidak dikirim, hanya 1R dan callback% | permintaan pengguna — qty dihitung sendiri sesuai modal saat itu |

Batasan yang diketahui sejak hari pertama:

- Data live pakai **Binance SPOT mirror**, bukan Binance perp — API perp
  diblokir dari runner GitHub (451) dan dari ISP Indonesia (timeout).
  Kecocokan sinyal terukur **96%**. Ini tracking error permanen.
- Strategi ini **gagal 2 dari 7 syarat kelayakan** di backtest (T1 OOS, T9 PBO).
  Forward test ini untuk mengumpulkan bukti, bukan tanda lampu hijau.
