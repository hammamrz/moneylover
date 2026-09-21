# moneylover

Pencatatan transaksi Money Lover secara otomatis dari email Gmail.

Email notifikasi bank dibaca, nominal dan tanggalnya diurai, transaksinya
dikategorikan, lalu dicatat ke dompet Money Lover yang sesuai — tanpa membuka
aplikasi.

```text
Gmail (notifikasi bank)
      │
      ▼
ekstraksi      aturan per penerbit: Bank Jago, wondr by BNI, Livin by Mandiri
      ▼
kategorisasi   77 kategori mengikuti pohon kategori akun, termasuk sub-kategorinya
      ▼
staging        JSON per bulan di automation/data/, ikut ter-commit sebagai jejak audit
      ▼
Money Lover    satu dompet per rekening, lewat API web-nya
```

Dokumentasi lengkap, cara menyiapkan kredensial, dan daftar perintahnya ada di
[`automation/README.md`](automation/README.md).

## Sekilas

```bash
python3 -m automation.cli check                    # verifikasi login, dompet, dan kategori
python3 -m automation.cli ingest --input email.json
python3 -m automation.cli review --month 2026-09
python3 -m automation.cli push --status ready      # dry-run; tambahkan --live untuk mengirim
python3 -m unittest discover -s automation/tests -t .
```

## Prinsip yang dipegang

- **Nominal dan tanggal tidak pernah ditebak model.** Keduanya melalui parser
  deterministik. Salah kategori mudah dibetulkan; salah satu digit di angka
  rupiah baru terasa berbulan-bulan kemudian.
- **Ragu berarti ditahan, bukan dikira-kira.** Transaksi yang dompetnya tidak
  dikenali atau nominalnya besar menunggu keputusan manusia di staging.
- **Kredensial hanya dari environment variable**, tidak pernah masuk ke repo.
