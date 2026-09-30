# Audit Eksternal Executor MEX 3.0: 30 September 2026

**Tujuan:** memeriksa kesiapan eksekusi uang asli di Hyperliquid sebelum
`MEX_EXEC_MODE` diganti ke `live`.

**Posisi auditor:** tim audit eksternal. Temuan dilaporkan dulu dalam tabel,
dan perbaikan baru dikerjakan setelah disetujui pemilik repo.

**Hasil singkat:**
- 18 temuan: 4 P0, 8 P1, 6 P2.
- 13 diperbaiki (#1, #2, #3, #5, #9, #10, #11, #12, #14, #15, #16 sebagian, #17, #18).
- 2 diputuskan tetap sesuai strategi (#4, #13).
- 3 ditunda: #6 dan #7 dikerjakan terpisah, #8 perlu verifikasi di akun live.

> **Strategi tidak disentuh.** `mex/strategy.py` tidak berubah satu baris pun,
> dan blok `strategy:` di `config.yaml` identik dengan versi yang dibacktest.
> Test strategi (23) tetap lulus sebelum dan sesudah audit.

| | |
|---|---|
| Repo | `daijobudesu69/Crypto-MEX` |
| Basis audit | `main` @ `2621ca8` (merge PR #4), sinkron dengan `d7b7a11` (commit state 12:08 UTC) |
| Commit perbaikan | `356f2e8` di branch `claude/tender-albattani-w7dxop` |
| Pull request | [PR #5](https://github.com/daijobudesu69/Crypto-MEX/pull/5): **open, belum di-merge**, CI hijau, mergeable (lihat §9) |
| Skala perubahan | 17 file, +874 / −97 baris (di luar laporan ini) |
| Test | **337 → 393 lulus** (lihat §7) |
| Temuan | 4 P0 · 8 P1 · 6 P2 |
| Keyakinan | Sistem **belum siap live** sebelum PR #5 di-merge (±85%). Sesudah merge, blokir P0 dari sisi kode sudah tertutup; sisa risikonya ada di §10. |

---

## Daftar isi

1. [Ringkasan eksekutif](#1-ringkasan-eksekutif)
2. [Cakupan, metode, dan batasan audit](#2-cakupan-metode-dan-batasan-audit)
3. [Pemahaman sistem](#3-pemahaman-sistem)
4. [Rencana pengembangan yang diaudit](#4-rencana-pengembangan-yang-diaudit)
5. [Temuan lengkap](#5-temuan-lengkap)
6. [Keputusan pemilik repo](#6-keputusan-pemilik-repo)
7. [Perubahan yang dikerjakan](#7-perubahan-yang-dikerjakan)
8. [Yang sengaja tidak diubah](#8-yang-sengaja-tidak-diubah)
9. [Pull request dan merge](#9-pull-request-dan-merge)
10. [Keterbatasan dan hal yang belum terverifikasi](#10-keterbatasan-dan-hal-yang-belum-terverifikasi)
11. [Checklist sebelum dan sesudah live](#11-checklist-sebelum-dan-sesudah-live)
12. [Panduan operasional](#12-panduan-operasional)
13. [Lampiran](#13-lampiran)

---

## 1. Ringkasan eksekutif

**Kondisi sebelum audit.** Executor (PR #4) sudah di-merge dan berjalan dalam
mode `dry`. Test suite hijau (337 test), tapi bursa tiruan di test tidak pernah
melempar exception, tidak menguji minimum order $10, dan tidak mensimulasikan
state yang hilang. Karena itu tiga bug P0 lolos, dan ketiganya terbukti dengan
proof-of-concept (PoC):

1. **Satu koin error menghentikan seluruh run.** Order yang sudah terkirim di
   run itu tidak tercatat dan tidak diumumkan. 12 koin lain berhenti diurus,
   berulang tiap 10 menit.
2. **`live.json` hilang atau basi membuat posisi menjadi "orphan".** Stop
   tertahan di 1R awal, tidak pernah di-trail, dan exit strategi tidak
   dieksekusi.
3. **Pindah dari `live` ke `dry` menelantarkan posisi tanpa peringatan.**

Temuan P0 keempat (#4, risiko agregat saat crash) adalah soal desain risiko,
bukan bug. Pemilik repo memutuskan tetap mengikuti strategi (§6).

**Kondisi sesudah audit (PR #5).** Semua P0 kode tertutup. Ditambah circuit
breaker 40% yang disetujui pemilik, minimum $10 dihitung di harga limit, alert
yang lebih andal, dan pembatasan akses kunci API. Test naik dari 337 ke 393,
dan CI di GitHub hijau.

**Yang masih menjadi risiko:**
- Edge strategi. Laporan proyek sendiri menempatkan ekspektasi realistis di
  antara skenario B (+53%/tahun) dan C (+12%/tahun).
- Kerugian ekor saat crash ketika banyak posisi searah terbuka (#4, diterima).
- Dua perilaku API Hyperliquid yang tidak bisa diverifikasi dari lingkungan
  audit (§10).

---

## 2. Cakupan, metode, dan batasan audit

### 2.1 Cakupan

| Area | File | Diperiksa |
|---|---|---|
| Executor | `mex/executor.py`, `mex/hl_client.py`, `run_executor.py` | seluruhnya, baris per baris |
| Sizing | `mex/execution.py` | seluruhnya |
| Sinyal & strategi | `run_signal.py`, `mex/strategy.py`, `mex/datafeed.py` | seluruhnya (strategi: dibaca, tidak diaudit ulang secara statistik) |
| Konfigurasi | `config.yaml`, `mex/config.py` | seluruhnya |
| State & I/O | `mex/ledger.py`, `tools/refresh_state.sh`, `tools/save_state.sh`, `.gitattributes` | seluruhnya |
| Notifikasi | `mex/notify.py`, `run_heartbeat.py` | bagian executor, escaping, heartbeat |
| Workflow | `signal.yml`, `heartbeat.yml`, `ci.yml` | seluruhnya |
| Dependency | `requirements.txt`, SDK `hyperliquid-python-sdk==0.24.0` | versi dipin; source SDK dibaca untuk `order`, `modify_order` (batchModify), `query_order_by_cloid`, `Cloid` |
| Test | `tests/test_executor.py`, `tests/test_hl_sdk.py`, dan lainnya | cakupan dan celahnya |
| Laporan proyek | artifact "MEX 3.0 Project Report" | dibaca sebagai konteks |

### 2.2 Metode

1. **Baseline.** Semua suite dijalankan di venv dengan dependency yang dipin:
   23 + 184 + 72 + 4 + 53 + 1 = **337 lulus**.
2. **Membaca kode secara adversarial.** Setiap jalur yang mengirim order ditelusuri
   untuk tiga kondisi: exception, state yang hilang, dan pergantian mode.
3. **PoC.** Skrip di scratchpad dijalankan terhadap `FakeHL` dari test suite
   (tanpa jaringan, tanpa kunci). Hasilnya ada di §13.1.
4. **Verifikasi SDK.** Kemampuan cloid, modify, dan query order dicek langsung
   di source SDK 0.24.0.
5. **Validasi perbaikan.** Seluruh suite dijalankan ulang (393 lulus), ditambah
   pyflakes pada file yang berubah (tidak ada temuan baru), `bash -n` pada
   script workflow, dan parsing YAML. CI GitHub pada PR #5 juga hijau.

### 2.3 Batasan (dinyatakan terbuka)

- **Tidak ada akses jaringan ke `api.hyperliquid.xyz` dari lingkungan audit**
  (proxy 403). Perilaku API live tidak bisa diuji: field `cloid` di open order,
  arti saldo di akun unified, dan acuan harga untuk minimum $10. Lihat §10.
- **Tidak ada akses ke akun atau kunci API.** Tidak ada satu order pun yang
  dikirim selama audit.
- **Angka backtest di laporan proyek tidak diverifikasi ulang.** Contohnya
  2.651 trade, +0,105 R, dan skenario A–D. Angka itu dipakai sebagai konteks
  apa adanya.
- **Status watcher, saldo 127.52 USDC, dan jadwal verifikasi 20:30 WIB** berasal
  dari laporan proyek, bukan observasi auditor.

---

## 3. Pemahaman sistem

### 3.1 Alur tiap cek (±10 menit)

Satu job GitHub Actions (`signal.yml`) hidup ±5,5 jam. Cron `7,37 * * * *`
hanya bertugas menyalakan job itu. Di dalamnya ada loop:

```
refresh_state.sh  -> git reset --hard origin/main (kalau tidak ada perubahan lokal)
run_signal.py     -> 13 koin, bar 4H, data Hyperliquid -> Gate.io -> Binance spot
                     menulis state/position.json, antre pesan Telegram (outbox)
run_executor.py   -> menyamakan akun Hyperliquid dengan state strategi
                     menulis state/live.json dan state/live_trades.csv
run_heartbeat.py  -> sekali sehari
save_state.sh     -> commit dan push state/ ke main (retry + rebase)
```

### 3.2 Strategi (tidak diubah, diringkas untuk konteks)

- **Trigger:** high > high tertinggi 20 bar sebelumnya **dan** volume > 1,5× SMA20.
- **Long:** EMA20 > EMA50, RSI14 > 55 dan naik dibanding 5 bar lalu.
- **Fade short:** EMA20 < EMA50 dan RSI di bawah puncak 20 bar atau sedang turun.
- **Entry:** di open bar berikutnya. Zona ±0,5R dan kedaluwarsa 8 jam dipakai
  untuk eksekusi manual.
- **Exit:** trailing stop saja, tanpa take profit. 1R = 1,5 × ATR14 candle entry.
  Callback dibekukan di 1R ÷ harga entry. Trail diukur dari high/low water mark
  dan **tidak pernah melonggar**.

### 3.3 Siklus hidup satu trade di executor

1. **Bar sinyal tutup.** `run_signal` membuat `pending`. Di cek yang sama,
   executor mengatur isolated 4x, mengirim order market (IOC, batas slippage 1%),
   lalu langsung memasang **stop sementara** di `fill − side × r_est` (1R dari
   ATR candle sinyal).
2. **Candle entry tutup.** Strategi membuat posisinya dan menghitung trail
   pertama dari ATR candle entry. Executor menggeser stop ke trail itu.
3. **Setiap candle 4H berikutnya tutup.** Stop digeser ke trail baru, dan trail
   ini hanya bisa mengetat.
4. **Keluar**, lewat salah satu dari dua jalan:
   - stop kena di bursa, lalu executor mencatatnya; atau
   - strategi exit, lalu executor menutup posisi dengan order market reduce-only.

**Mode (`MEX_EXEC_MODE`):**
- `off`: tidak melakukan apa pun.
- `dry`: default. Setelah audit, posisi yang sudah terbuka tetap dijaga.
- `manage`: saklar darurat, tanpa entry baru.
- `live`: trading penuh.

### 3.4 Parameter eksekusi

- Venue Hyperliquid, isolated 4x.
- Risiko 1% dari saldo USDC live, diambil dari spot clearinghouse karena akun
  memakai mode unified.
- Order di bawah $10 dinaikkan ke $10, kecuali risikonya jadi lebih dari 2× target.
- Cadangan margin 5%.
- API wallet `MEX.bot` berlaku sampai 2027-03-28.

---

## 4. Rencana pengembangan yang diaudit

Sesuai laporan proyek §11:

| # | Rencana | Relevansi audit |
|---|---|---|
| 1 | Job watcher baru dengan executor | Kode baru aktif setelah PR #5 di-merge (§9) |
| 2 | Verifikasi terjadwal 20:30 WIB | Dari sesi sebelumnya; bukan bagian dari audit ini |
| 3 | Dry run sekitar 10 sinyal | Dry run hanya menguji **sizing**, tidak menguji endpoint trading maupun trail (#6, #7) |
| 4 | Switch ke `live` | **Sebaiknya setelah PR #5 di-merge** |
| 5 | Pantau 3 trade live pertama | Tambahkan verifikasi §10 ke checklist |
| 6 | Harga exit dan fee asli dari `userFills` | Belum dikerjakan (§10) |
| 7 | PnL live di heartbeat | Belum dikerjakan |
| 8 | Review bulanan | — |
| 9 | Cek trailing stop native di API tiap kuartal | — |
| 10 | Review koin (Mar–Apr 2027) | Lihat catatan simbol yang dihapus di §10 |
| 11 | Perpanjangan API wallet sebelum 2027-03-28 | Heartbeat sudah mengingatkan mulai 14 hari sebelumnya |

---

## 5. Temuan lengkap

**Prioritas:**
- **P0:** wajib diselesaikan sebelum `live`.
- **P1:** sebelum atau segera setelah `live`.
- **P2:** nanti.

"Yakin" adalah keyakinan auditor bahwa temuannya benar.

| # | Prio | Temuan | Dampak uang | Bukti | Yakin | Status |
|---|---|---|---|---|---|---|
| 1 | **P0** | Satu simbol error (koin di-rename, harga hilang, timeout) membatalkan seluruh run executor | Order yang sudah terkirim tidak tercatat dan tidak diumumkan; semua simbol sesudahnya tidak dikelola; berulang tiap 10 menit. Preseden nyata: TON → GRAM di Hyperliquid, 2026. | **PoC** (§13.1); `executor.py` loop tanpa try per simbol | 95% | ✅ Diperbaiki |
| 2 | **P0** | `live.json` hilang/basi (job mati atau push gagal) membuat posisi jadi orphan | Stop tertahan di 1R awal, tidak di-trail, exit strategi tidak dieksekusi. PoC: stop tetap 115.62 padahal trail 130. | **PoC**; `executor.py` jalur "terlewat" dan `_orphans` | 90% | ✅ Diperbaiki (cloid + adopsi) |
| 3 | **P0** | Mode live → `dry`/`off` saat ada posisi | Trail dan exit berhenti tanpa alert; posisi "entering" yang dipulihkan di mode dry tidak diberi stop | **PoC**; gerbang `mode in ("live","manage")` | 95% | ✅ Diperbaiki |
| 4 | **P0** | Risiko agregat: margin semua posisi bisa sampai 95% saldo | Crash searah (seperti 10 Okt 2025) bisa melikuidasi semua posisi isolated, ±89% akun (contoh di §6.1) | `MARGIN_HEADROOM` | 85% | ⚖️ Diterima: sama dengan strategi |
| 5 | P1 | Telegram executor tanpa outbox, retry, atau escaping HTML | Alarm "posisi tanpa stop" hilang karena satu timeout atau karena teks error berisi `<`/`&` | `run_executor.py` (kirim tanpa cek), teks `{st}`/`{r}` mentah | 90% | ✅ Diperbaiki |
| 6 | P1 | Dry run tidak menguji endpoint trading | Masalah signing, nonce, atau asset dari runner GitHub baru ketahuan di trade uang asli pertama | Desain mode `dry` | 90% | ⏳ Ditunda (mode canary) |
| 7 | P1 | Dry run tidak menguji logika trail/exit | 10 sinyal dry hanya memvalidasi sizing | Desain mode `dry` | 85% | ⏳ Ditunda (mode shadow) |
| 8 | P1 | Semantik saldo `total` di akun unified | Kalau margin isolated sudah dikurangi dari `total`, margin terhitung dua kali dan kapasitas mengecil | `hl_client.usdc_balance`, `free = balance - used` | 50% | 🔍 Verifikasi di akun live |
| 9 | P1 | Minimum $10 untuk short dihitung di mid | Harga limit IOC short 1% di bawah mid, jadi order bisa bernilai $9,90 dan ditolak | `_enter` sizing | 65% | ✅ Diperbaiki |
| 10 | P1 | `live.json` rusak dibaca di luar try | Executor crash tiap cycle tanpa alert | `run_executor.py` | 95% | ✅ Diperbaiki |
| 11 | P1 | Pesan sinyal memakai `capital_usd: 100`, executor memakai saldo live | Perbandingan dry run (rencana langkah 3) pasti menemukan selisih palsu | `run_signal._sizing` | 95% | ✅ Diperbaiki |
| 12 | P1 | Celah test: `FakeHL` tanpa exception, tanpa minimum $10, tanpa state hilang | Bug #1, #9, #10 lolos | `tests/test_executor.py` | 95% | ✅ Diperbaiki |
| 13 | P2 | Stop bisa turun saat berpindah dari stop sementara ke stop strategi | Risiko nyata bisa sedikit di atas 1% (contoh di §6.2) | `_keep_stop` | 75% | ⚖️ Diterima: sama dengan strategi |
| 14 | P2 | Alert posisi tanpa stop hanya 1× per 24 jam | Kondisi paling berbahaya hanya dilaporkan sekali sehari | `REALERT` | 90% | ✅ Diperbaiki (1 jam) |
| 15 | P2 | Order trigger reduce-only buatan user (mis. TP) dianggap stop bot | TP manual bisa di-cancel atau diubah jadi stop bot | `stop_orders` + `_keep_stop` | 85% | ✅ Diperbaiki |
| 16 | P2 | Kunci agent ada di env seluruh step; dependency transitif tidak dipin | Risiko supply chain. Agent tidak bisa withdraw, tapi bisa trading. | `signal.yml`, `requirements.txt` | 80% | ◐ Sebagian: kunci dibatasi; lockfile berhash belum |
| 17 | P2 | `heartbeat.yml` cadangan tidak mengirim `MEX_EXEC_MODE` | Heartbeat menampilkan "dry" walau sedang live, justru saat watcher mati | `run_heartbeat._executor_line` | 95% | ✅ Diperbaiki |
| 18 | P2 | Tidak ada circuit breaker (drawdown akun) | Kalau edge hilang (skenario D), bot trading terus sampai dihentikan manual | — | 80% | ✅ Ditambahkan (40%) |

---

## 6. Keputusan pemilik repo

Semua keputusan diambil pemilik repo pada 30 Sep 2026. Prinsipnya: **entry dan
exit harus sama dengan strategi yang dibacktest.**

### 6.1 #4 Risiko agregat: tetap, sama dengan strategi

Contoh dengan saldo $127.52 dan risiko 1% ≈ $1.28 per trade. Median jarak stop
3,65%, jadi order ≈ $35 dan margin 4x ≈ $8.7. Saldo cukup untuk 13 posisi
sekaligus (13 × $8.7 ≈ $113).

| Kejadian | Hitungan | Rugi |
|---|---|---|
| 13 posisi **terbuka bersamaan**, semua kena stop normal | 13 × $1.28 | $16.6 (13,0%) |
| 13 trade **berurutan**, rugi satu per satu (compounding) | $127.52 × (1 − 0,99¹³) | $15.6 (12,2%) |
| Crash > 30% dalam satu candle, stop terlewati, semua posisi long terlikuidasi | 13 × $8.7 | ±$113 (±89%) |

Selisih antara "bersamaan" dan "berurutan" kecil. Semua posisi yang terbuka
bersamaan dihitung dari saldo yang sama karena belum ada yang direalisasikan
rugi. Kasus crash jarang terjadi, tapi dampaknya hampir seluruh akun. Pemilik
memilih untuk **tidak** menambah batas di luar strategi.

### 6.2 #13 Stop sementara dan stop strategi: tetap, sama dengan strategi

Di bursa hanya ada **satu stop aktif pada satu waktu**:

| Waktu | Stop aktif | Contoh (long SOL, fill $120) |
|---|---|---|
| Fill sampai candle entry tutup | **Stop sementara**. Dihitung dari ATR candle sinyal, karena ATR candle entry belum diketahui. Backtest sama sekali tidak punya stop di candle ini. | 1R = $4.38 → **$115.62** |
| Candle entry tutup | **Stop strategi pertama**: max(entry − 1R, high × (1 − callback)), 1R dari ATR candle entry | ATR 3.2 → 1R $4.80, callback 4%; high $120.20 → **$115.39** |
| Setiap candle berikutnya | Stop strategi, hanya bisa naik | — |

Dengan ukuran 0,29 SOL, rugi di $115.62 ≈ $1.27, dan di $115.39 ≈ $1.34
(1,05%). Trail strategi tidak pernah turun. Yang turun hanya perpindahan dari
stop sementara (estimasi) ke nilai pertama stop strategi. Opsi mengunci "stop
tidak pernah di bawah stop sementara" ditawarkan, dan pemilik memilih tidak.

### 6.3 #18 Circuit breaker: 40% dari puncak saldo

Drawdown terdalam per skenario (dari simulasi di laporan proyek, bukan data live):

| Skenario | Return/tahun | Drawdown terdalam |
|---|---|---|
| A (13 koin terpilih, bias seleksi) | +181% | 24% |
| B (tanpa bias, rata-rata 30 koin) | +53% | 39% |
| C (edge meluruh ke 21%) | +12% | 50% |
| D (edge hilang) | −12% | 58% |

**Alasan memilih 40%:** angka ini di atas drawdown skenario realistis B, jadi
breaker tidak mematikan strategi yang masih untung. Breaker baru aktif kalau
hasil lebih buruk dari B. Batas 25% kemungkinan besar sudah aktif di skenario
B. Keyakinan auditor pada angka ini ±55%, karena berasal dari simulasi.

**Cara kerja:**
- Puncak saldo dicatat di `live.json`.
- Saat saldo ≤ puncak × 60%, entry baru berhenti. Posisi yang terbuka tetap
  dijaga sampai selesai.
- Alert dikirim saat breaker aktif, lalu diulang sekali sehari.
- Breaker tidak mati sendiri walau saldo naik lagi.
- Reset lewat repo variable `MEX_BREAKER_RESET` (§12). **Wajib dijalankan
  setelah withdraw**, karena withdraw terbaca sebagai drawdown.

### 6.4 #9 Minimum $10: disetujui

Minimum Hyperliquid $10 berlaku untuk semua koin, termasuk HYPE. Masalahnya ada
di acuan harga:
- Order short dikirim sebagai IOC dengan limit 1% di bawah mid.
- Kalau minimum dihitung dari harga limit, order yang ukurannya tepat $10 di
  mid hanya bernilai $9,90, lalu ditolak.

Perbaikannya: ukuran dihitung supaya ukuran × harga limit ≥ $10. Hanya berlaku
untuk order yang dinaikkan ke $10 (stop > ~12,7% pada saldo $127) atau yang
ukurannya $10–$10,10. Order short itu jadi ±1% lebih besar. Perbaikan ini aman
apa pun acuan harga yang dipakai bursa.

### 6.5 #11 Pesan sinyal memakai saldo live: disetujui

---

## 7. Perubahan yang dikerjakan

### 7.1 Per file

| File | Perubahan |
|---|---|
| `mex/hl_client.py` | **Cloid bot** berawalan `0x4d4558` ("MEX") dengan kode jenis: 01 entry, 02 stop, 03 close. Cloid entry deterministik dari `simbol\|signal_id`; stop/close unik per order. `ioc_px()`. `market`, `place_stop`, `modify_stop` menerima cloid. `stop_orders` ikut membaca `cloid`. `entry_filled(cloid)` lewat `orderStatus`. |
| `mex/executor.py` | try/except **per simbol** (`Result.errors`, alert 1 jam kalau ada posisi di simbol itu). **Adopsi** posisi saat state hilang/basi, dengan bukti cloid entry. Catatan basi dari trade lama ditutup tanpa menutup posisi baru. **Semua mode selain `off` menjaga posisi terbuka**; `dry` tetap tanpa entry baru dan mengirim alert. **Circuit breaker** (`_breaker`). Minimum $10 di harga limit. `_bot_stops`: hanya order dengan cloid bot atau oid yang tercatat. Arah stop dicek. Semua teks bursa di-`esc()`. `_alert(every=…)` dan `REALERT_URGENT` = 1 jam. `_finish` aman kalau `r_est` kosong. |
| `run_executor.py` | **Outbox** Telegram (retry tiap run, TTL 24 jam). `live.json` rusak → exit 2 + alert maks. 1×/jam lewat `state/live_alerts.json`, file tidak ditimpa. Mode `off` dengan posisi live → alert. Hasil sebagian tetap disimpan saat exception. `MEX_BREAKER_RESET` diteruskan. Exit 1 kalau ada simbol error. |
| `mex/execution.py` | `live_balance(account)` lewat endpoint publik `spotClearinghouseState`, tanpa kunci. `Sizing.capital_live`. |
| `run_signal.py` | `_sizing` memakai saldo live (1 request per run, hanya saat ada sinyal); `capital_usd` jadi cadangan. |
| `mex/notify.py` | Blok sizing: "saldo $X" untuk saldo live, atau "modal $100 (perkiraan, saldo live tidak terbaca)". |
| `mex/config.py` | Kunci `execution.max_drawdown_pct` divalidasi: angka, 0 < x < 100, bukan bool. |
| `config.yaml` | `max_drawdown_pct: 40` beserta alasan dan cara reset. Komentar `capital_usd` diperbarui. **Blok `strategy:` tidak berubah.** |
| `run_heartbeat.py` | Baris executor menampilkan status circuit breaker. |
| `.github/workflows/signal.yml` | Kunci API dipindah ke variabel shell yang tidak di-export, lalu `unset`; hanya diteruskan ke `run_executor.py`. `MEX_BREAKER_RESET` dipetakan. |
| `.github/workflows/heartbeat.yml` | `MEX_EXEC_MODE` diteruskan. |
| `README.md`, `CHANGELOG.md` | Dokumentasi mode, circuit breaker, adopsi, cloid; entri CHANGELOG 30 Sep. |
| `tests/*` | Lihat §7.2. |

### 7.2 Test

| Suite | Sebelum | Sesudah | Tambahan utama |
|---|---|---|---|
| `test_strategy.py` | 23 | 23 | — (parity strategi tidak berubah) |
| `test_infra.py` | 184 | 196 | validasi `max_drawdown_pct`, parsing saldo live dan fallback, pesan saldo live/perkiraan, baris heartbeat breaker |
| `test_executor.py` | 72 | 116 | cloid bot; isolasi error per simbol + alert 1 jam; dry menjaga posisi; adopsi (hilang saat posisi, hilang saat pending, posisi manual tidak diadopsi, state basi); minimum $10 di harga limit; order manual tidak disentuh; circuit breaker (aktif, blokir entry, tetap trail, reset, tanpa config); escaping; driver (outbox, error jaringan, off + posisi, `live.json` rusak) |
| `test_pipeline_invariants.py` | 4 | 4 | saldo live di-stub supaya tetap offline |
| `test_backtest_hyperliquid.py` | 53 | 53 | — |
| `test_hl_sdk.py` | 1 | 1 | cloid bot lolos validasi SDK dan ikut ke wire (`"c"`), 13 koin × 3 skala × 2 arah |
| **Total** | **337** | **393** | |

`FakeHL` sekarang bisa: melempar exception per method, menolak order di bawah
$10 berdasarkan harga limit, mencatat cloid, menjawab `entry_filled`, dan
mengembalikan error berisi HTML.

---

## 8. Yang sengaja tidak diubah

- `mex/strategy.py`, blok `strategy:` di `config.yaml`, logika sinyal, entry,
  dan trail.
- Stop sementara saat fill dan perpindahannya ke stop strategi (#13).
- Leverage 4x, cadangan margin 95%, risiko 1%, aturan $10 dengan batas 2× (#4).
- Aturan "sinyal yang terlewat tidak dikejar".
- Mode `manage` dan `off` (kecuali alert tambahan di `off`).

---

## 9. Pull request dan merge

| | |
|---|---|
| PR | [daijobudesu69/Crypto-MEX#5](https://github.com/daijobudesu69/Crypto-MEX/pull/5): "Audit executor: keandalan eksekusi + circuit breaker 40%" |
| Head → base | `claude/tender-albattani-w7dxop` (`356f2e8`) → `main` (`d7b7a11`) |
| Dibuat | 2026-09-30 13:01 UTC |
| CI | `unit` ✅ sukses (13:01–13:02 UTC) · `connectivity` ✅ sukses |
| Status merge | **open, belum di-merge**, `mergeable_state: clean` (tidak ada konflik) |
| Konflik dengan commit state | Tidak ada. PR tidak menyentuh `state/`. |

### 9.1 Cara merge

Merge tidak terikat ke sesi chat mana pun. Pilih salah satu:
1. **Di GitHub:** buka PR #5, klik **Merge pull request**. Bisa dari browser
   atau aplikasi HP.
2. **Sesi Claude lain:** minta "merge PR #5 di Crypto-MEX".
3. **Sesi audit ini:** minta "merge".

### 9.2 Yang terjadi sesudah merge

- **Kode Python aktif di cek berikutnya (≤10 menit).** `refresh_state.sh`
  melakukan reset ke `main` tiap cek, dan kode baru kompatibel dengan loop dan
  env job lama. Job lama belum punya `MEX_BREAKER_RESET`, jadi reset breaker
  baru bisa dipakai mulai job berikutnya.
- **Perubahan `signal.yml` berlaku mulai job berikutnya**, paling lama ±5,5 jam.
  Sampai saat itu, kunci API masih terlihat di env seluruh step, seperti sebelum
  audit.
- **Sisa dari mode `dry` di `live.json`.** Sinyal yang sudah ditangani dalam
  mode dry tetap tercatat "handled", jadi tidak di-entry ulang saat pindah ke live.
- **Kunci baru di `live.json`.** `peak_balance`, `breaker`,
  `breaker_reset_seen`, dan `outbox` ditambahkan otomatis ke file yang sudah ada.

---

## 10. Keterbatasan dan hal yang belum terverifikasi

| # | Hal | Kenapa belum pasti | Dampak kalau asumsinya salah | Cara memverifikasi |
|---|---|---|---|---|
| V1 | `frontendOpenOrders` mengembalikan field `cloid` | Jaringan ke Hyperliquid diblokir dari lingkungan audit | Stop bot tetap dilacak lewat oid. Yang tidak jalan: mengenali stop lama setelah state hilang. Bisa muncul stop kedua (keduanya reduce-only). | Trade live pertama: lihat order stop di respons API atau log |
| V2 | Arti `total` saldo spot di akun unified saat ada posisi isolated (#8) | Sama | Kapasitas margin salah hitung (terlalu konservatif atau terlalu longgar); dasar sizing dan circuit breaker ikut bergeser | Bandingkan `live.json` / log dengan UI saat ada posisi terbuka |
| V3 | Acuan harga minimum $10 (limit atau mid) | Sama | Tidak ada: perbaikan aman untuk keduanya | — |
| V4 | `orderStatus` untuk cloid lama masih bisa dijawab | Retensi riwayat order tidak diketahui | Adopsi gagal lalu kembali ke perilaku lama: alert orphan, tidak ada kerusakan baru | Uji sekali setelah trade live pertama |
| V5 | Circuit breaker memakai saldo USDC, bukan equity dengan PnL mengambang | Tergantung V2 | Breaker bereaksi setelah rugi direalisasikan, tidak saat PnL mengambang | Diterima; catat di review bulanan |
| V6 | Adopsi dari state basi | Harga exit trade lama tidak diketahui | Baris EXIT trade lama memakai harga stop terakhir (perkiraan) | Butuh `userFills` (rencana #6) |
| V7 | #6 canary dan #7 shadow belum dibuat | Ditunda atas kesepakatan | Endpoint trading dari runner dan logika trail baru teruji dengan uang asli | Kerjakan sebelum live, atau terima risikonya dengan ukuran awal kecil |
| V8 | #16 lockfile berhash belum dibuat | Di luar scope yang disetujui | Dependency transitif SDK bisa berubah di rilis baru | `pip-compile --generate-hashes` via PR |
| V9 | GitHub Actions dipin dengan tag (`@v4`, `@v5`), bukan SHA | Tidak masuk temuan utama | Risiko supply chain kecil | Pin ke SHA |
| V10 | Harga exit live memakai level stop, fee tidak dihitung | Sudah diketahui sejak laporan proyek | PnL/R live di Telegram dan CSV hanya perkiraan | Rencana #6 (`userFills`) |
| V11 | Simbol yang dihapus dari `INSTRUMENTS` saat masih ada posisi live | Executor hanya melakukan loop atas `SYMBOLS` | Posisi di simbol itu tidak lagi dikelola | Saat review koin: tutup posisi dulu, baru hapus simbol |
| V12 | Angka backtest dan skenario laporan proyek | Tidak diaudit ulang | Pemilihan 40% dan ekspektasi return bergantung pada angka itu | Bandingkan dengan forward test/live tiap bulan |

**Risiko strategi (bukan kode):**
- Validasi awal gagal di OOS decay (21%) dan PBO.
- 13 koin dipilih setelah hasil backtest diketahui (bias seleksi).
- Ekspektasi realistis ada di antara skenario B dan C. Keyakinan auditor bahwa
  edge bertahan di live: **±35%**.

---

## 11. Checklist sebelum dan sesudah live

**Sebelum `MEX_EXEC_MODE=live`:**
- [ ] Merge PR #5.
- [ ] Log watcher menampilkan `[exec] mode=dry selesai` tanpa `simbol error`.
- [ ] `state/live.json` di `main` berisi `peak_balance` ≈ saldo akun.
- [ ] Heartbeat harian berisi baris executor, dan mode-nya benar.
- [ ] Beberapa sinyal dry: angka di pesan sinyal ("saldo $…") sama dengan
      rencana DRY-RUN (ukuran, stop, margin).
- [ ] Opsional tapi disarankan: #6 canary (order ALO $10 jauh dari harga, lalu
      cancel) untuk membuktikan endpoint trading dari runner.
- [ ] Tunggu job baru mulai setelah merge, supaya pembatasan kunci API di
      `signal.yml` aktif.

**3 trade live pertama:**
- [ ] Fill tercatat; stop terlihat di Open Orders sebagai reduce-only Stop
      Market dengan cloid berawalan `0x4d4558` (verifikasi **V1**).
- [ ] Stop bergeser setelah tiap candle 4H tutup, dan tidak ada stop ganda.
- [ ] Saldo di log dibandingkan dengan UI saat posisi terbuka (verifikasi **V2**).
- [ ] Baris ENTRY dan EXIT di `state/live_trades.csv`.
- [ ] Tidak ada alert `executor error`.

---

## 12. Panduan operasional

```bash
# Mode
gh variable set MEX_EXEC_MODE --body dry    --repo daijobudesu69/Crypto-MEX   # tanpa entry baru; posisi live tetap dijaga
gh variable set MEX_EXEC_MODE --body live   --repo daijobudesu69/Crypto-MEX   # trading penuh
gh variable set MEX_EXEC_MODE --body manage --repo daijobudesu69/Crypto-MEX   # saklar darurat
gh variable set MEX_EXEC_MODE --body off    --repo daijobudesu69/Crypto-MEX   # tidak melakukan apa pun (ada alert kalau masih ada posisi)

# Circuit breaker: lanjutkan setelah aktif, ATAU set ulang puncak setelah withdraw.
# Nilai apa saja yang belum pernah dipakai (mis. tanggal-jam); jalan di PowerShell.
# (Semula `--body $(date +%s)`, yang hanya jalan di bash -- diganti 2026-09-30.)
gh variable set MEX_BREAKER_RESET --body reset-20261001-0900 --repo daijobudesu69/Crypto-MEX

# Test lokal
python tests/test_strategy.py && python tests/test_infra.py && python tests/test_executor.py
python tests/test_pipeline_invariants.py && python tests/test_backtest_hyperliquid.py
CI=1 python tests/test_hl_sdk.py
```

**Arti alert baru di Telegram:**

| Alert | Arti | Tindakan |
|---|---|---|
| 🚨 `X: executor error … cek manual` | Satu simbol gagal diproses, dan ada posisi live di simbol itu. Diulang tiap jam. | Cek posisi dan stop di UI |
| ⚠️ `catatan posisi live hilang … diambil alih lagi` | State hilang; posisi dibuktikan milik bot lewat cloid dan diadopsi | Tidak ada; cek stop di UI |
| ⚠️ `Mode dry, tapi ada posisi live` | Posisi tetap dijaga, entry baru berhenti | Pastikan mode yang dipilih memang disengaja |
| ⚠️ `executor mode off` + posisi | Posisi **tidak** dijaga | Ganti ke `manage` atau `dry` |
| 🛑 `Circuit breaker AKTIF` | Saldo turun ≥ 40% dari puncak | Evaluasi strategi dulu, baru reset |
| ⚠️ `executor berhenti: state/live.json rusak` | Executor tidak jalan sama sekali | Perbaiki atau pulihkan file dari riwayat git |

---

## 13. Lampiran

### 13.1 Hasil PoC (sebelum perbaikan, terhadap `FakeHL`)

```
PoC1: whole run aborted: KeyError 'SOL renamed' | ETH entered? True
      -> order ETH terkirim, tapi baris ENTRY/Telegram-nya hilang, dan simbol sesudah SOL tidak diproses
PoC2 dry after live: exch pos still open: True | events: [] | rows: 0
      -> strategi exit, posisi live tidak ditutup, tanpa alert
PoC3 lost live.json: ['SKIPPED'] ['⚠️ Ada posisi SOL di akun yang tidak dibuka bot…'] | stop trigger: [115.62]
      -> trail strategi 130, stop tertahan di 115.62
PoC4 alert text contains raw error repr: … Cek manual! {'error': 'Order has invalid price'}
      -> teks bursa masuk ke HTML Telegram tanpa escape
```

Setelah perbaikan, keempat skenario ini menjadi test permanen di
`tests/test_executor.py`.

### 13.2 Format cloid

```
0x 4d4558 01 <24 hex sha256(simbol|signal_id)>              entry  (deterministik)
0x 4d4558 02 <12 hex hash> <12 hex waktu µs mod 2^48>        stop   (unik per order)
0x 4d4558 03 <12 hex hash> <12 hex waktu µs mod 2^48>        close  (unik per order)
```

Total 16 byte (32 hex), sesuai validasi `Cloid` di SDK 0.24.0.

### 13.3 Aturan untuk asisten berikutnya

- Aturan strategi dan `config.yaml → strategy` dibekukan. Setiap perubahan
  config atau aturan dicatat di `CHANGELOG.md` beserta tanggal dan alasannya.
- Jangan pernah meminta, membaca, mencetak, atau meng-commit kunci agent.
- Default mode `dry`. Hanya pemilik repo yang mengganti ke `live`.
- Semua perubahan lewat branch dan PR supaya CI jalan dulu.
- Kode yang di-merge langsung jalan di job watcher yang sedang aktif, jadi harus
  tetap kompatibel dengan loop dan env lama.
