# Crypto-MEX — Forward Test

Forward test langsung untuk strategi **Momentum Exhaustion Breakout (MEX)** di
ETHUSDT perpetual, timeframe 4H. GitHub Actions memeriksa tiap jam, mengirim
sinyal ke Telegram **hanya kalau ada**, dan mencatat semuanya ke CSV di repo ini.

> [!WARNING]
> **Strategi ini GAGAL 2 dari 7 syarat kelayakan di backtest** (T1 out-of-sample
> dan T9 overfitting). Repo ini ada untuk mengumpulkan bukti out-of-sample yang
> jujur — **bukan** untuk dipakai dengan uang sungguhan. Paper trade dulu.
> Detail lengkap ada di `MEX_BACKTEST_REPORT.md` di luar repo ini.

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
mex/strategy.py     aturan sinyal + state machine trailing stop
mex/indicators.py   EMA/RMA/RSI/ATR gaya Pine (Wilder), disalin dari engine backtest
mex/datafeed.py     ambil data + failover + sanity check
mex/ledger.py       CSV append-only + webhook Google Sheets
mex/notify.py       pengirim Telegram + template pesan
run_signal.py       driver tiap jam
run_heartbeat.py    driver harian
config.yaml         parameter (jangan diubah tanpa mencatat di CHANGELOG)
state/              state + log, di-commit balik oleh workflow
```

**`tests/test_strategy.py` adalah pengamannya.** Dia memutar ulang 4.000 bar
Binance perp asli dan memastikan sinyalnya **identik bit-per-bit** dengan engine
backtest yang lulus T0–T14. Kalau file ini merah, repo sudah menyimpang dari
strategi yang divalidasi — jangan percayai sinyalnya sampai hijau lagi.

Tes lain menjaga: trailing stop tidak pernah mundur (ratchet), exit selalu
dibenarkan trail bar **sebelumnya** (bebas lookahead), tidak ada exit di bar
entry, dan `callback% == 1R/harga` selalu konsisten.

---

## Jadwal

| Workflow | Kapan | Kirim pesan? |
|---|---|---|
| `signal.yml` | tiap jam, menit ke-10 | **hanya kalau ada** sinyal/entry/exit |
| `heartbeat.yml` | 01:30 UTC (08:30 WIB) | 1× sehari, selalu |
| `ci.yml` | tiap push | tidak |

Jalan tiap jam walau lilin 4H, karena run yang tertunda akan menyusul sendiri di
run berikutnya. Bar yang sudah diproses dilewati — **tidak ada pesan dobel.**

Perkiraan volume pesan: **~3,7 sinyal/bulan** (≈11 pesan/bulan termasuk
konfirmasi entry & exit) + 30 heartbeat. Bisa saja seminggu penuh tanpa sinyal.

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
| `GSHEET_WEBHOOK_URL` | opsional | URL Apps Script (lihat `docs/google-sheets.md`) |

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
