# Logger otomatis ke Google Sheets

CSV di `state/` tetap sumber kebenaran. Google Sheets hanya cermin, supaya
mudah dilihat dan difilter dari HP. Kegagalan kirim ke Sheets tidak pernah
menggagalkan run.

## Langkah

1. Buat spreadsheet baru. Buat **tiga sheet** dengan nama persis:
   `events`, `trades`, `runs`.
2. `Extensions → Apps Script`, hapus isinya, tempel `apps_script.gs` dari folder ini.
3. `Deploy → New deployment → Web app`
   - Execute as: **Me**
   - Who has access: **Anyone**  ← wajib, GitHub Actions memanggil tanpa login
4. Salin URL-nya (`https://script.google.com/macros/s/.../exec`).
5. Simpan sebagai secret repo bernama `GSHEET_WEBHOOK_URL`.

Header kolom dibuat otomatis dari baris pertama yang masuk, jadi tidak perlu
disiapkan manual.

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

Tempel di sheet `trades`, kolom kosong di sebelah kanan:

```
Total R          =SUM(result_R)
Expectancy       =AVERAGE(result_R)
Win rate         =COUNTIF(result_R,">0")/COUNT(result_R)
Profit factor    =SUMIF(result_R,">0")/ABS(SUMIF(result_R,"<0"))
Rugi beruntun    (pakai kolom bantu; lihat catatan di bawah)
Slippage rata2   =AVERAGE(actual_fill_price-entry_price)
Efek keterlambatan =CORREL(signal_to_send_minutes, result_R)
Give-back rata2  =AVERAGE(giveback_pct)
```

Bandingkan `AVERAGE(result_R)` dengan patokan backtest **+0,31 R**. Kalau
setelah 30+ transaksi angkanya jauh di bawah itu, forward test sedang
mengonfirmasi kecurigaan T1/T9 — dan itu temuan yang berharga, bukan kegagalan.
