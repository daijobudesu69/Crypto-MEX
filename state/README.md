# state/

Ditulis otomatis oleh workflow dan di-commit balik ke repo. Jangan diedit
manual kecuali kolom `actual_*` dan `notes` di `trades.csv`.

| File | Isi |
|---|---|
| `position.json` | bar terakhir diproses, posisi terbuka, sinyal tertunda |
| `events.csv` | satu baris per SIGNAL / ENTRY / EXIT + snapshot indikator |
| `trades.csv` | satu baris per transaksi selesai — tabel evaluasi |
| `runs.csv` | satu baris per run: liveness, latensi, sumber data |

Kosong sampai workflow pertama jalan.
