# Logger otomatis ke Google Sheets

CSV di `state/` tetap sumber kebenaran. Google Sheets hanya cermin, supaya mudah
dilihat dan difilter dari HP. Kegagalan menulis ke Sheets tidak pernah
menggagalkan run.

Ada dua cara. **Pilih salah satu.** Kalau keduanya diisi, service account menang.

---

## Cara A — Service account (dipakai sekarang)

Akses langsung ke Sheets API. Tidak ada endpoint publik, dan aksesnya bisa
dicabut kapan saja dengan meng-unshare spreadsheet-nya.

### Langkah

1. **Buat spreadsheet baru.** Tab tidak perlu disiapkan — `events`, `trades`,
   dan `runs` dibuat otomatis lengkap dengan headernya.

2. **Ambil ID spreadsheet** dari URL-nya:
   ```
   https://docs.google.com/spreadsheets/d/1AbC...XyZ/edit
                                          ^^^^^^^^^^^ ini ID-nya
   ```

3. **Bagikan spreadsheet ke service account.** Klik Share, tempel alamat
   `client_email` dari file JSON kunci Anda (bentuknya
   `nama@project.iam.gserviceaccount.com`), beri akses **Editor**.
   Tanpa langkah ini API akan menolak dengan 403 — dan log akan menyebutkan
   alamat mana yang perlu dibagikan.

4. **Aktifkan Google Sheets API** di project yang sama:
   Google Cloud Console → APIs & Services → Library → "Google Sheets API" → Enable.

5. **Simpan dua secret repo:**

   | Secret | Isi |
   |---|---|
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | isi file kunci JSON, apa adanya |
   | `GSHEET_SPREADSHEET_ID` | ID dari langkah 2 |

> [!CAUTION]
> Kunci service account itu kredensial sungguhan. Jangan pernah menempelkannya
> ke kolom yang meminta URL, jangan commit ke repo, dan **rotasi segera** kalau
> pernah muncul di log mana pun. Masking secret GitHub **tidak menutupi nilai
> multi-baris**, jadi kunci JSON tidak terlindungi otomatis di log Actions.
> Kode di repo ini karena itu tidak pernah mencetak isi secret — exception
> dilaporkan berdasarkan tipenya saja.

---

## Cara B — Apps Script webhook

Lebih sederhana, dan **tidak ada kunci rahasia sama sekali**. Yang disimpan cuma
sebuah URL; kalau bocor, paling parah orang bisa menambah baris ke spreadsheet
Anda — bukan mengakses akun Google Anda.

1. Buat spreadsheet, lalu buat tiga sheet: `events`, `trades`, `runs`.
2. `Extensions → Apps Script`, hapus isinya, tempel `apps_script.gs` dari folder ini.
3. `Deploy → New deployment → Web app` — Execute as **Me**, Access **Anyone**.
4. Salin URL `.../exec`, simpan sebagai secret `GSHEET_WEBHOOK_URL`.

---

## Membaca kolom `sheet_ok` di `runs.csv`

| Nilai | Artinya |
|---|---|
| `ok` | semua baris run itu berhasil ditulis |
| `partial_2/3` | sebagian gagal — cek log run tersebut |
| `failed` | semua gagal ditulis |
| `unreachable` | tidak ada baris untuk ditulis, dan probe koneksi gagal |
| `not_configured` | tidak ada secret Sheets yang diisi |

Kolom ini melaporkan **hasil pengiriman sebenarnya**, bukan sekadar apakah
secret-nya terisi — webhook yang mati diam-diam adalah persis kegagalan yang
kalau tidak begini bisa lolos berminggu-minggu.

---

## Kolom yang Anda isi sendiri

Empat kolom terakhir sengaja kosong. Isi setelah Anda benar-benar entry/exit:

| Kolom | Isi dengan |
|---|---|
| `actual_fill_price` | harga fill Anda yang sebenarnya |
| `actual_qty` | ukuran posisi yang Anda pakai |
| `actual_exit_price` | harga keluar Anda yang sebenarnya |
| `notes` | apa pun — kenapa dilewat, kondisi pasar, perasaan saat itu |

`actual_fill_price` dikurangi `entry_price` adalah **slippage nyata Anda**.
Tidak ada backtest yang bisa menghasilkan angka itu; hanya forward test bisa.

## Rumus evaluasi yang berguna

Tempel di sheet `trades`, di kolom kosong sebelah kanan:

```
Total R            =SUM(result_R)
Expectancy         =AVERAGE(result_R)
Win rate           =COUNTIF(result_R,">0")/COUNT(result_R)
Profit factor      =SUMIF(result_R,">0")/ABS(SUMIF(result_R,"<0"))
Slippage rata2     =AVERAGE(actual_fill_price-entry_price)
Efek keterlambatan =CORREL(signal_to_send_minutes, result_R)
Give-back rata2    =AVERAGE(giveback_pct)
```

Bandingkan `AVERAGE(result_R)` dengan patokan backtest **+0,31 R**. Kalau setelah
30+ transaksi angkanya jauh di bawah itu, forward test sedang mengonfirmasi
kecurigaan T1/T9 — dan itu temuan berharga, bukan kegagalan.
