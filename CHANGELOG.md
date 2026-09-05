# Changelog

Setiap perubahan pada `config.yaml` atau aturan strategi WAJIB dicatat di sini
dengan tanggal dan alasan. Forward test yang parameternya diubah diam-diam di
tengah jalan tidak membuktikan apa pun.

## 2026-09-05 — heartbeat ikut pindah ke pemantau, + bug bar diproses ulang

**Parameter strategi tidak diubah.**

Perbaikan 2026-09-04 hanya memindahkan penjadwalan **sinyal** ke pemantau;
`heartbeat.yml` dibiarkan pakai cron polos `7 0 * * *`. Akibatnya heartbeat —
satu-satunya pesan yang datang tiap hari, jadi yang paling terasa — masih telat
**~4 jam setiap hari**: terukur 4j02m, 4j07m, 4j10m, 4j06m empat hari berturut-
turut (terburuk 7j04m pada 31 Ags), sampai ke HP pukul ~11:13 WIB padahal README
menjanjikan 07:07 WIB. Slot 00:0x UTC adalah yang paling padat di antrean GitHub.

- **Heartbeat dikirim dari loop pemantau.** `run_heartbeat.py` sekarang punya
  `_due()` yang memutuskan sendiri apakah hari ini sudah terkirim
  (`last_heartbeat_date` di state) dan apakah sudah lewat jam target
  (`MEX_HEARTBEAT_UTC`, default 00:00 UTC = 07:00 WIB). Aman dipanggil tiap 10
  menit: pengecekan terjadi **sebelum** menyentuh jaringan, jadi 143 dari 144
  panggilan harian berhenti seketika. Hasilnya heartbeat datang ≤10 menit dari
  target, bukan ~4 jam.
- Hari hanya ditandai terkirim kalau Telegram benar-benar menerimanya; kirim
  gagal membiarkan harinya terbuka supaya tick berikutnya mencoba lagi.
  Sebelumnya heartbeat yang gagal hilang sampai cron besok.
- `heartbeat.yml` tetap ada sebagai **cadangan** kalau pemantau mati — momen yang
  justru paling perlu diketahui — dijadwalkan `23 1,5,9 * * *` di jam lebih sepi,
  dan menolak kirim kalau pemantau sudah mengirim hari itu. Ada input `force`
  untuk mengirim manual kapan saja.
- `merge_state.py` ikut menjaga `last_heartbeat_date` (tanggal terbaru menang);
  tanpa itu, konflik rebase bisa menghapus tandanya dan memicu heartbeat kedua.

**Bug bar diproses ulang (ditemukan saat mengukur latensi).** Job pemantau
checkout sekali lalu hidup 5,5 jam; saat pergantian job, job baru bisa checkout
sebelum push terakhir job lama mendarat, membaca `position.json` basi, dan
memproses ulang bar yang sudah selesai. Terjadi 2× dalam 24 jam:

    20:06  run 33878095203  last_bar 16:00  bars_processed=1   <- job lama
    20:16  run 33911360144  last_bar 16:00  bars_processed=1   <- job baru, bar SAMA

Kebetulan bar-bar itu tidak menghasilkan event. Tapi kalau menghasilkan SIGNAL,
`sent_ids` di checkout basi juga belum berisi id-nya — **pesan Telegram dobel**.
`tools/refresh_state.sh` menyinkronkan working tree ke origin di awal tiap
iterasi, dengan dua penjaga supaya tidak pernah membuang pekerjaan: dilewati
kalau ada perubahan `state/` belum ter-commit, dan kalau ada commit lokal belum
ter-push.

`tests/test_infra.py` naik jadi 56 test.

## 2026-09-04 — cron diganti pemantau hidup (H1 dari audit)

**Parameter strategi tidak diubah.** `expiry_hours` tetap 8.0.

Sinyal LONG pertama (`20260903T1200-L`, bar 12:00 UTC) terkirim dengan benar —
`telegram_ok=sent`, 11,6 menit setelah lilin tutup, ENTRY-nya menyusul juga. Jadi
mesin pengirim hasil perbaikan 2026-09-03 terbukti bekerja pada sinyal nyata
pertamanya.

Tapi pengukuran ulang menunjukkan penjadwalannya masih berbahaya: dari ~46 jadwal
dalam 26 jam hanya **12 yang berjalan (26%)**, dan lubang 11:31–16:19 UTC
(**4j48m**) menelan HABIS jendela kirim bar 08:00. Kalau sinyalnya muncul satu bar
lebih awal, sinyal itu hilang permanen. Sinyal yang benar-benar terjadi selamat
hanya karena kebetulan.

Menambah baris cron tidak menolong — yang bermasalah GitHub yang tidak
menjalankannya. Jadi polanya dibalik:

- **`signal.yml` sekarang pemantau, bukan pengecek sekali jalan.** Job hidup
  ~5j30m (batas keras GitHub 6 jam) dan menjalankan `run_signal.py` tiap 10 menit
  dari dalam. Cron `7,37 * * * *` tugasnya cuma MENYALAKAN ulang; satu yang
  berhasil sudah menutupi 5,5 jam berikutnya, dan jadwal yang jatuh saat pemantau
  hidup akan antre lalu langsung mulai setelahnya. Repo ini publik sehingga menit
  Actions tidak dibatasi.
- Latensi kirim turun dari median 92 menit jadi **≤10 menit** setelah lilin tutup.
- `workflow_dispatch` dapat input `mode`: `once` (default, satu kali cek) atau
  `loop`.

Pendukungnya:

- **`tools/save_state.sh`** — logika commit/push/rebase diekstrak dari kedua
  workflow supaya bisa dipanggil berulang dari dalam loop, dan supaya logika
  konflik yang rapuh ini hanya ada di satu tempat.
- Skrip itu sekarang juga **memulihkan commit yang belum ter-push** dari iterasi
  sebelumnya. Versi pertamanya keluar 0 saat tidak ada perubahan baru, yang akan
  meninggalkan commit gagal-push menggantung selamanya sambil melapor sukses —
  kelas kegagalan yang sama dengan `rebase --skip` yang sudah dibuang kemarin.
- **`MEX_QUIET_IDLE`** menahan baris `runs.csv` untuk run yang tidak memproses bar
  apa pun sampai 60 menit berlalu. Tanpa ini, cek tiap 10 menit berarti 144 baris
  dan 144 commit per hari. Run yang memproses bar, menghasilkan event, atau gagal
  selalu dicatat.

`tests/test_infra.py` naik jadi 42 test.

## 2026-09-03 — audit infrastruktur: pengiriman, state, penjadwalan

**Parameter strategi TIDAK diubah sama sekali.** `mex/strategy.py` tidak
disentuh, dan blok `strategy:` di `config.yaml` masih identik dengan baseline
yang dibacktest — termasuk `expiry_hours: 8.0`, yang sengaja dibiarkan meski
jeda cron terburuk yang terukur (8j 45m) sudah melewatinya. Semua perubahan di
bawah ada di pipa yang mengantarkan sinyal, bukan pada aturan yang menghasilkannya.

Pemicunya: audit menemukan seluruh jalur pengiriman SIGNAL **belum pernah
dieksekusi di produksi** (32 dari 32 run sinyal berstatus `no_events`), sementara
`runs.csv` sendiri menunjukkan cron hanya berjalan 23% dari jadwal.

Kritis — sinyal bisa hilang atau dobel:

- **Outbox + dedup pengiriman.** Setiap pesan ditulis ke `state.outbox` dulu dan
  baru dihapus setelah Telegram menerimanya; kuncinya lalu masuk `state.sent_ids`.
  Sebelumnya satu timeout 25 detik menghilangkan sinyal secara permanen sementara
  job tetap melaporkan sukses. `last_bar` tetap selalu maju — `step()` adalah
  state machine dan mengulang bar yang sama akan merusak trailing stop — jadi
  pengiriman kini dilacak terpisah supaya bisa diulang tanpa memutar ulang strategi.
- **Job merah saat pengiriman gagal** (`exit 1`), supaya GitHub langsung mengirim
  email. Pesannya tetap di outbox dan tetap dicoba ulang tiap run.
- **`rebase --skip` dihapus** dari langkah simpan state. Fallback itu membuang
  commit state run ini sementara loop tetap mencetak "state tersimpan" — ledger
  dan Telegram jadi tidak sinkron tanpa peringatan.
- **Teks exception di-escape** sebelum masuk pesan `parse_mode=HTML`. Alert
  kegagalan feed sebelumnya ditolak Telegram dengan `400 can't parse entities`
  justru ketika feed benar-benar mati.
- **`if: always()`** pada langkah simpan state, supaya run yang gagal tidak lagi
  menghapus baris `runs.csv`-nya sendiri.

Tinggi — ketahanan state:

- `write_json` atomik (`tmp` + `os.replace`); `read_json` melempar `StateCorrupt`
  alih-alih diam-diam mengembalikan default — default akan tampak seperti run
  pertama dan meninggalkan posisi terbuka tanpa stop.
- Header CSV divalidasi; log dengan skema lama dirotasi ke `events.v1.csv`
  daripada dirusak permanen oleh baris berkolom baru.
- Konflik `position.json` diselesaikan berdasarkan **isi** lewat
  `tools/merge_state.py` (`last_bar` terbaru menang, `sent_ids` dan `outbox`
  digabung), bukan berdasarkan sisi rebase. `--theirs` dulu justru memilih state
  yang lebih lama dalam jalur retry dan bisa memicu kirim ulang.
- `fetch-depth: 0` — rebase di atas clone dangkal bisa gagal menemukan merge base,
  artinya jalur pemulihan konflik rusak tepat saat dibutuhkan.

Perubahan `config.yaml` (bukan parameter strategi):

- **`symbol` dan `timeframe` dihapus.** `datafeed.fetch()` tidak pernah menerima
  keduanya, jadi mengisi `symbol: BTCUSDT` hanya mengganti label di CSV dan pesan
  Telegram sementara data yang diunduh tetap ETH (diverifikasi langsung: BTC
  77.833 vs 2.391 yang benar-benar dikembalikan). Instrumen sekarang dikunci di
  `mex/datafeed.py` (`SYMBOL` / `INTERVAL`), dan `config.load()` menolak kunci
  lama itu supaya kesalahan yang sama tidak bisa terulang diam-diam.
- **`bootstrap_flat` dihapus** — dibaca ke dalam cfg lalu tidak pernah dipakai.

Lain-lain: pesan ENTRY dan EXIT kini benar-benar dikirim (sebelumnya fungsinya
ada tapi tidak pernah dipanggil, padahal README menjanjikannya); dependensi dipin
ke versi yang terbukti jalan; job konektivitas dipisah dari unit test supaya
outage pihak ketiga tidak membuat badge merah seolah strateginya rusak;
`tests/test_infra.py` (34 test) mengunci semua perilaku di atas.

`ENGINE_VERSION` naik ke `mex-fwd-1.1.0` supaya baris ledger sebelum dan sesudah
perubahan ini bisa dipisahkan saat evaluasi.

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
