# Crypto-MEX — Forward Test

Forward test langsung untuk strategi **Momentum Exhaustion Breakout (MEX)** di
ETHUSDT perpetual, timeframe 4H. GitHub Actions memeriksa tiap jam, mengirim
sinyal ke Telegram **hanya kalau ada**, dan mencatat semuanya ke CSV di repo ini.

> [!WARNING]
> **Strategi ini GAGAL 2 dari 7 syarat kelayakan di backtest** (T1 out-of-sample
> dan T9 overfitting). Repo ini ada untuk mengumpulkan bukti out-of-sample yang
> jujur — **bukan** untuk dipakai dengan uang sungguhan. Paper trade dulu.
>
> Riwayat lengkap — hasil validasi T0–T14, alasan tiap keputusan eksekusi,
> insiden yang pernah terjadi, dan apa yang masih belum diketahui — ada di
> **[`docs/PROJECT_LOG.md`](docs/PROJECT_LOG.md)** (bahasa Inggris).

> [!NOTE]
> **Audit infrastruktur Sep 2026 — [`docs/AUDIT-2026-09.md`](docs/AUDIT-2026-09.md).**
> 23 temuan di pipa yang mengantarkan sinyal (pengiriman Telegram, state,
> penjadwalan); 22 sudah diperbaiki. Aturan strateginya sendiri tidak disentuh.
> Baca itu sebelum mengubah apa pun di `run_signal.py`, `mex/ledger.py`, atau
> workflow — tiap perbaikan di sana ada alasannya, dan alasannya dicatat.

---

## Aturan strategi

Dievaluasi saat lilin 4H **tutup**. Eksekusi di **open lilin berikutnya**.

**Pemicu dasar (wajib untuk kedua arah):**

```
high  >  high tertinggi dari 20 lilin SEBELUMNYA   (lilin berjalan tidak dihitung)
volume >  1,5 × SMA(volume, 20)
```

| | LONG | FADE SHORT |
|---|---|---|
| Tren | EMA20 > EMA50 | EMA20 < EMA50 |
| RSI | > 55 **dan** naik vs 5 lilin lalu | < puncak RSI 20 lilin **atau** turun vs 5 lilin lalu |
| Posisi | harus kosong | harus kosong |

Short dipicu breakout ke **atas** saat tren turun — breakout melawan tren
dianggap jebakan, jadi dilawan. Ini disengaja, bukan bug.

## Exit — trailing stop, tidak ada TP

Satu level saja, dan **tidak pernah bergerak melawan posisi**:

```
1R            = 1,5 × ATR(14) di bar entry
callback rate = 1R ÷ harga entry × 100     ← dibekukan di entry
stop          = MAX(stop lama, harga tertinggi − callback%)
```

Di bawah harga entry dia stop loss; di atas harga entry dia pengunci profit.
Satu order **Trailing Stop** di Binance, dipasang sekali, tidak disentuh lagi.

**Callback rate dihitung ulang tiap transaksi.** Di backtest rentangnya
1,09%–5,24%. Memakai satu angka tetap (misal rata-ratanya, 2,75%) memangkas
return per tahun dari +14,24% jadi +7,62%.

Ukuran posisi: `qty = (risiko% × modal) ÷ 1R`. Dengan risiko 1%, kerugian
maksimum ≈ 1% modal.

## Zona entry — kenapa ada rentang

Antrean GitHub Actions bisa menunda cron sampai berjam-jam. Sinyal karena itu
dikirim sebagai **rentang**, bukan satu harga:

- masih sah selama harga dalam **±0,5R** dari harga referensi
- **hangus setelah 8 jam** dari lilin sinyal

Sinyal yang sudah hangus **tetap dicatat** di `state/events.csv` (ditandai
`EXPIRED_BEFORE_SEND`) tapi tidak dikirim, dan entry/exit-nya juga tidak
diumumkan. Jadi forward test tetap lengkap tanpa Anda dibanjiri pesan basi.

---

## Sumber data — dan tracking error-nya

`fapi.binance.com` menjawab **HTTP 451** dari runner GitHub (IP US diblokir
Binance) dan **ConnectTimeout** dari ISP Indonesia. Jadi data Binance perp asli
— yang dipakai backtest — **tidak bisa diakses**.

Diukur terhadap 3.636 bar Binance perp (Jan 2025 – Ags 2026):

| Sumber | Sinyal long cocok | Beda harga | Beda ATR |
|---|---|---|---|
| **`data-api.binance.vision`** (Binance SPOT) — utama | **110/115 = 96%** | 0,046% | 1,91% |
| `api.gateio.ws` (Gate.io perp) — cadangan otomatis | 102/115 = 89% | 0,008% | 0,88% |

**Ini tracking error nyata dan tidak bisa dihilangkan.** Sekitar 1 dari 25
sinyal akan berbeda dari yang backtest hasilkan. Sumber yang dipakai dicatat di
setiap baris log supaya bisa dipisahkan saat evaluasi.

`tests/test_connectivity.py` ikut mengecek apakah `fapi.binance.com` sudah bisa
diakses. Kalau suatu hari bisa, pindah ke sana dan tracking error ini hilang.

---

## Isi repo

```
mex/strategy.py       aturan sinyal + state machine trailing stop
mex/indicators.py     EMA/RMA/RSI/ATR gaya Pine (Wilder), disalin dari engine backtest
mex/datafeed.py       ambil data + failover + sanity check; SYMBOL/INTERVAL dikunci di sini
mex/ledger.py         CSV append-only + webhook Google Sheets
mex/notify.py         pengirim Telegram + template pesan
run_signal.py         driver tiap jam
run_heartbeat.py      driver harian
tools/merge_state.py  penyelesai konflik state saat dua run bertabrakan
tools/save_state.sh   commit/push state dengan retry + penggabungan
config.yaml           parameter strategi (jangan diubah tanpa mencatat di CHANGELOG)
state/                state + log, di-commit balik oleh workflow
docs/AUDIT-2026-09.md audit infrastruktur: 23 temuan + perbaikannya
docs/PROJECT_LOG.md   riwayat validasi T0-T14 dan keputusan eksekusi
```

> [!NOTE]
> **Instrumen dan timeframe tidak ada di `config.yaml`.** Repo ini dikunci ke
> ETHUSDT 4H lewat `SYMBOL` / `INTERVAL` di `mex/datafeed.py`. Dulu ada kunci
> `symbol` dan `timeframe` di config, tapi `fetch()` tidak pernah menerima
> keduanya — mengisinya hanya mengganti label di CSV dan Telegram sementara data
> yang diunduh tetap ETH. `config.load()` sekarang menolak kunci itu.

**`tests/test_strategy.py` adalah pengamannya.** Dia memutar ulang 4.000 bar
Binance perp asli dan memastikan sinyalnya **identik bit-per-bit** dengan engine
backtest yang lulus T0–T14. Kalau file ini merah, repo sudah menyimpang dari
strategi yang divalidasi — jangan percayai sinyalnya sampai hijau lagi.

Tes lain menjaga: trailing stop tidak pernah mundur (ratchet), exit selalu
dibenarkan trail bar **sebelumnya** (bebas lookahead), tidak ada exit di bar
entry, dan `callback% == 1R/harga` selalu konsisten.

**`tests/test_infra.py` menjaga pipa pengirimnya** — outbox, dedup, penulisan
state atomik, escaping HTML, rotasi header CSV, dan penggabungan state saat
rebase. Tiap tes di situ mewakili satu cacat nyata yang pernah ditemukan; kalau
merah, salah satu cacat itu kembali.

---

## Jadwal

| Workflow | Kapan | Kirim pesan? |
|---|---|---|
| `signal.yml` | pemantau hidup ~5,5 jam, cek tiap **10 menit** | **hanya kalau ada** sinyal/entry/exit |
| `heartbeat.yml` | 00:07 UTC (07:07 WIB) | 1× sehari, selalu |
| `ci.yml` | tiap push + konektivitas harian 06:17 UTC | tidak |

### Kenapa pemantau, bukan cron biasa

Cron GitHub tidak dijamin jalan, dan pada repo ini keandalannya terukur **23–26%**:
dari ~46 jadwal dalam 26 jam, hanya 12 yang benar-benar berjalan. Pada 3 Sep 2026
ada lubang **4 jam 48 menit** (11:31–16:19 UTC) yang menelan habis seluruh jendela
kirim bar 08:00 — sinyal di bar itu akan hilang tanpa jejak.

Menambah baris cron tidak menolong: yang bermasalah bukan jadwalnya, tapi GitHub
yang tidak menjalankannya. Jadi polanya dibalik:

> **Cron tidak lagi bertugas mengecek. Tugasnya hanya menyalakan pemantau, yang
> lalu hidup ~5,5 jam dan mengecek sendiri tiap 10 menit dari dalam.**

Satu cron yang berhasil sudah menutupi 5,5 jam berikutnya. Jadwal yang jatuh saat
pemantau masih hidup akan **antre** (GitHub menyimpan satu run pending per
concurrency group) lalu langsung mulai begitu yang lama habis — jadi cakupannya
nyaris nonstop meski sebagian besar cron tetap dibuang GitHub. Batas keras job
GitHub adalah 6 jam; anggaran loop 5j30m menyisakan ruang untuk penyimpanan akhir.

Repo ini publik, dan **menit Actions untuk repo publik tidak dibatasi**, jadi job
panjang tidak memakan kuota apa pun.

Efeknya pada latensi: sinyal kini terkirim dalam **≤10 menit** setelah lilin
tutup, bukan median 92 menit seperti sebelumnya.

Mau cek cepat satu kali? Actions → `MEX signal` → **Run workflow** → mode `once`.
Bar yang sudah diproses dilewati, jadi aman dijalankan kapan saja.

Perkiraan volume pesan: **~3,7 sinyal/bulan** (≈11 pesan/bulan termasuk
konfirmasi entry & exit) + 30 heartbeat. Bisa saja seminggu penuh tanpa sinyal.

### Jaminan pengiriman

Dua daftar di `state/position.json` yang membuat pipeline ini aman diulang:

- **`outbox`** — pesan ditulis ke sini dulu, dan baru dihapus setelah Telegram
  menerimanya. Kirim yang gagal tetap tersimpan dan dicoba lagi tiap run sampai
  berhasil atau sinyalnya hangus. Sebelum ada ini, satu timeout menghilangkan
  sinyal selamanya sementara job tetap hijau.
- **`sent_ids`** — kunci setiap pesan yang sudah terkirim. Run yang mati setelah
  mengirim tapi sebelum menyimpan state akan diputar ulang di run berikutnya, dan
  daftar ini yang memastikan pemutaran ulang itu **tidak mengirim dua kali**.

`last_bar` selalu maju melewati setiap bar yang sudah dilihat `step()`, bahkan
ketika pengiriman gagal — `step()` adalah state machine, memberinya bar yang sama
dua kali akan merusak trailing stop. Karena itu pengiriman dilacak terpisah.

Kalau ada pesan yang tersangkut di outbox, **job-nya merah** (supaya GitHub
mengirim email) dan heartbeat harian ikut menyebutkannya.

> [!NOTE]
> **Kalau pemantau mati, cakupan bergantung lagi pada cron.** Job bisa dibunuh
> runner, kehabisan waktu, atau gagal start. Cron `7,37 * * * *` yang menyalakan
> ulang tetap tunduk pada keandalan GitHub yang ~25%, jadi jeda terburuk sebelum
> pemantau berikutnya hidup masih bisa beberapa jam. Bedanya sekarang: lubang itu
> harus terjadi **tepat saat tidak ada pemantau yang hidup**, bukan tiap kali cron
> meleset. Heartbeat harian akan memperlihatkannya lewat `bar terakhir diproses`
> yang tertinggal.
>
> Lapis berikutnya kalau ini masih kurang: pemicu eksternal (cron-job.org atau
> Cloudflare Worker) yang memanggil `workflow_dispatch` lewat API GitHub memakai
> Personal Access Token ber-scope `actions:write`.

---

## Setup

### 1. Izin token (sekali saja)

```bash
gh auth refresh -h github.com -s workflow
```

### 2. Secrets

`Settings → Secrets and variables → Actions → New repository secret`

| Secret | Wajib? | Isi |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | untuk notifikasi | token dari [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | untuk notifikasi | chat id Anda (dari [@userinfobot](https://t.me/userinfobot)) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | opsional | isi file kunci JSON service account |
| `GSHEET_SPREADSHEET_ID` | opsional | ID spreadsheet, dari URL-nya |
| `GSHEET_WEBHOOK_URL` | opsional | alternatif tanpa kunci: URL Apps Script |

Untuk Sheets, pilih **salah satu** cara — service account (dua secret pertama)
atau Apps Script webhook. Langkahnya di [`docs/google-sheets.md`](docs/google-sheets.md).

> [!CAUTION]
> Kunci service account adalah kredensial. Masking secret GitHub **tidak
> menutupi nilai multi-baris**, jadi kunci JSON tidak terlindungi otomatis di
> log Actions. Kode di repo ini tidak pernah mencetak isi secret, tapi kalau
> kunci Anda pernah muncul di log mana pun — **rotasi segera**.

Tanpa secret Telegram, pipeline tetap jalan penuh dan pesan dicetak ke job log —
berguna untuk menguji sebelum bot-nya jadi. **Kegagalan kirim Telegram tidak
pernah menggagalkan run**, supaya gangguan messaging tidak terlihat seperti
gangguan strategi.

### 3. Aktifkan

Actions → `MEX signal` → **Run workflow**. Run pertama adalah *bootstrap*:
mengunci bar terakhir dan mulai dari posisi kosong, **tanpa** mengirim sinyal
historis. Sinyal asli mulai dari bar berikutnya.

---

## Membaca hasilnya

- **`state/trades.csv`** — satu baris per transaksi selesai. Ini tabel evaluasi.
- **`state/events.csv`** — satu baris per SIGNAL/ENTRY/EXIT + snapshot indikator
  di bar itu. Untuk menjawab *kenapa* sinyal muncul, bukan cuma *bahwa* dia muncul.
- **`state/runs.csv`** — satu baris per run. Liveness, latensi, sumber data.

Empat kolom sengaja dikosongkan untuk **Anda isi sendiri**:
`actual_fill_price`, `actual_qty`, `actual_exit_price`, `notes`. Selisih antara
fill Anda dan `entry_price` referensi adalah slippage nyata Anda — angka yang
tidak bisa dihasilkan backtest mana pun.

Kolom `signal_to_send_minutes` merekam keterlambatan tiap sinyal, jadi nanti bisa
dijawab: *apakah sinyal yang telat hasilnya lebih buruk?*

### Kapan hasilnya bisa dinilai

**Jangan menilai dari 10–20 transaksi pertama.** Di backtest, transaksi *tipikal*
hasilnya nyaris nol (median +0,06R di fase bull, −0,19R di fase sideways) —
profitnya datang dari segelintir transaksi besar. Dengan ~3,7 transaksi/bulan,
sampel yang layak dinilai butuh **minimal 6 bulan**, idealnya 12.

Patokan dari backtest: expectancy **+0,31 R** per transaksi, win rate **~45%**,
hold rata-rata **20 jam**, siap-siap **11 kali rugi beruntun**.

---

## Mengubah parameter

Jangan diam-diam. Setiap perubahan `config.yaml` **wajib** dicatat di
`CHANGELOG.md` dengan tanggal dan alasan. Forward test yang parameternya diubah
di tengah jalan tidak membuktikan apa pun — dan menyetel parameter setelah
melihat hasil live adalah bentuk overfitting yang persis dilarang di spec §10.
