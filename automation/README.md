# Automation: Gmail → pencatatan transaksi

Lanjutan dari Routine **Kelola Gmail pribadi**. Routine itu memberi label
`Trade` pada email transaksi; pipeline di folder ini mengubah email berlabel
tersebut menjadi baris transaksi yang siap dicatat.

## Alur

```text
Gmail (label Trade)
      │  agen Routine mengambil email mentah
      ▼
extract.py      regex per penerbit + fallback generic  → nominal, tanggal, merchant, rekening
      ▼
categorize.py   aturan kata kunci + deteksi transfer internal
      ▼
ledger.py       staging JSON per bulan di automation/data/ (ikut ter-commit)
      ▼
exporters.py / adapters/moneylover.py   → CSV, backup My CashFlow+, atau API Money Lover
```

Yang **tidak** dikerjakan model: membaca nominal dan tanggal. Keduanya selalu
lewat `normalize.py` yang deterministik, karena satu digit meleset di angka
rupiah lebih berbahaya daripada salah kategori.

## Kenapa ada tahap staging, bukan langsung tulis ke Money Lover

1. Container Routine berumur pendek. Tanpa staging di git, tidak ada catatan
   transaksi mana yang sudah diproses, sehingga run berikutnya bisa dobel.
2. Deduplikasi butuh riwayat. Satu transaksi sering dikirim dua email
   (notifikasi bank + struk merchant), dan transfer antar rekening sendiri
   memunculkan sepasang debit/kredit yang harus digabung jadi satu baris.
3. Transaksi bernominal besar atau berkategori meragukan sebaiknya dilihat
   manusia dulu. Status `needs_review` menahan baris itu dari impor otomatis.

## Batasan Money Lover yang perlu diketahui

* Money Lover **tidak punya API publik** dan **tidak punya impor CSV resmi**;
  impor resminya memakai berkas `.mlx` dari fitur backup.
* `adapters/moneylover.py` memakai endpoint internal `web.moneylover.me/api`
  yang dipakai aplikasi webnya. Endpoint ini bisa berubah sewaktu-waktu.
* Autentikasi memakai JWT sesi web lewat environment variable
  `MONEYLOVER_JWT`. Token berumur sekitar satu minggu, dan satu akun hanya
  boleh memegang lima token perangkat sekaligus.
* Endpointnya berada di belakang Cloudflare, yang menolak permintaan dengan
  signature klien bawaan `urllib` (error 1010). Adapter karena itu mengirim
  header yang sama seperti halaman webnya.
* Satu akun hanya boleh memegang lima perangkat, dan setiap login mendaftarkan
  satu. Token hasil login disinggahkan di direktori sementara container
  (`MONEYLOVER_TOKEN_CACHE` untuk mengubah lokasinya) supaya satu run yang
  memanggil `check` lalu `push` tetap hanya sekali login. Bila muncul
  "Maximum device limit reached", hapus perangkat lama lewat pengaturan
  aplikasi Money Lover.
* Karena itu mode bawaan adapter adalah **dry-run**. Tidak ada kredensial yang
  disimpan di repo.

### Kategori berbeda di tiap dompet

Money Lover menyimpan daftar kategori per dompet, bukan satu daftar global.
Contoh dari akun ini: Superbank punya 136 kategori, Gopai hanya 106. Sebuah
sub-kategori yang tidak ada di dompet tujuan karena itu dikirim ke induknya,
misalnya `Buy Saham` menjadi `Investment` dan `Salary` menjadi `Co. Office`.
Artinya tetap terjaga, bukan jatuh ke `Other Expense`.

Bila sub-kategori maupun induknya tidak ada di dompet itu, transaksinya
ditahan dan `cli check` menyebutnya di daftar `menghambat`. Perbaikannya:
buat kategori tersebut di dompet yang bersangkutan lewat aplikasi Money
Lover.

## Menyiapkan Money Lover (wajib sebelum `--live`)

1. Buka <https://web.moneylover.me> di browser dan login seperti biasa.
2. Pilih salah satu cara autentikasi:

   **a. Email dan password** (tidak butuh browser sama sekali). Set
   `MONEYLOVER_EMAIL` dan `MONEYLOVER_PASSWORD`. Adapter menukarnya sendiri
   menjadi token lewat endpoint OAuth Money Lover setiap kali dibutuhkan,
   jadi tidak ada token yang perlu diperbarui manual. Konsekuensinya password
   akun tersimpan di environment, dan tiap login menambah satu perangkat
   terdaftar di akun Money Lover (batasnya lima; bersihkan lewat pengaturan
   aplikasi bila penuh).

   **b. Token sesi web.** Set `MONEYLOVER_JWT`. Ambil lewat DevTools → tab
   **Network** → klik request ke `/api/...` → header **Authorization**,
   isinya `AuthJWT eyJhbGciOi...`. Awalan `AuthJWT ` boleh ikut tersalin.
   Cara ini tidak menyimpan password, tetapi tokennya hanya berumur sekitar
   seminggu dan tidak tersedia bila DevTools dimatikan kebijakan perangkat.

   Bila keduanya diset, token yang dipakai dan login dilewati.
3. Simpan token itu sebagai environment variable **`MONEYLOVER_JWT`** pada
   environment Claude Code yang menjalankan Routine (Settings → Environments →
   Environment variables). Jangan pernah menaruhnya di dalam repo.
4. Pastikan `own_accounts` di `config.json` memetakan setiap rekening ke
   dompet Money Lover-nya (lihat **Satu dompet per rekening** di bawah).
5. Verifikasi:

   ```bash
   python3 -m automation.cli check
   ```

   Perintah ini melaporkan daftar dompet, kategori yang ada di Money Lover, dan
   kategori mana dari `rules/categories.json` yang **belum** punya pasangan di
   sana. Setiap kategori yang belum ada harus dibuat dulu di Money Lover, atau
   dipetakan ke nama lain lewat `moneylover.category_map`.

**Bila memakai cara (b), token kedaluwarsa kira-kira seminggu sekali.**
`check` melaporkan sisa harinya dan metode autentikasi yang dipakai, jadi
tidak perlu menebak. Bila run harian gagal dengan HTTP 401, ambil token baru
atau pindah ke cara (a). Ini konsekuensi memakai API yang tidak resmi, dan
tidak bisa dihindari tanpa menyimpan password akun.

## Perintah

```bash
# 1. Masukkan email mentah (array JSON) ke staging
python3 -m automation.cli ingest --input emails.json

# 2. Lihat antrean yang butuh keputusan manusia
python3 -m automation.cli review --month 2026-09

# 3a. Ekspor CSV untuk direview di spreadsheet / importer pihak ketiga
python3 -m automation.cli export --format moneylover-csv --out transaksi.csv

# 3b. Gabungkan ke backup My CashFlow+ lalu impor lewat Settings > Import backup
python3 -m automation.cli export --format cashflowplus \
    --base backup_asli.json --out backup_gabungan.json

# 3c. Kirim ke Money Lover (dry-run dulu, --live untuk benar-benar mengirim)
python3 -m automation.cli check
python3 -m automation.cli push --status ready
python3 -m automation.cli push --status ready --live

# 4. Putuskan kategori transaksi yang tertahan; statusnya langsung jadi ready
python3 -m automation.cli decide <fingerprint> --category makan

# 5. Tandai perjalanan dinas, kategorinya dihitung ulang
python3 -m automation.cli tag <fingerprint> --note "perjalanan dinas"

# 6. Tandai setelah dicatat manual
python3 -m automation.cli mark <fingerprint> --status imported
```

Hanya transaksi berstatus `ready` yang dikirim otomatis. Transaksi
`needs_review` — nominal besar, kategori belum ditentukan, atau layout email
yang belum dikenali — tetap menunggu keputusan Anda.

Alur keputusannya: `review` menampilkan antrean beserta fingerprint tiap
baris, lalu `decide <fingerprint> --category <kunci>` menetapkan kategorinya
dan melepasnya sekaligus. `--category` yang tidak dikenal tidak mengubah apa
pun dan mencetak daftar kategori yang tersedia. Bila kategorinya sudah benar
dan yang menahan hanya nominal besar, `mark <fingerprint> --status ready`
sudah cukup.

Merchant yang belum dikenali selalu tertahan, berapa pun confidence-nya.
Confidence mengukur keyakinan pada nominal dan tanggal, bukan pada kategori:
email yang terurai sempurna tapi merchantnya asing justru bernilai tinggi,
sehingga ambang confidence saja tidak pernah menahannya. Kalau satu merchant
berulang tiap bulan, tambahkan kata kuncinya ke `rules/categories.json` supaya
tidak perlu diputuskan lagi.

Format input `ingest`:

```json
[
  {
    "id": "gmail message id",
    "threadId": "gmail thread id",
    "from": "noreply@bca.co.id",
    "subject": "Notifikasi Transaksi BCA",
    "date": "2026-09-18T14:35:00+07:00",
    "body": "isi email, boleh HTML",
    "hints": { "amount": "Rp125.000", "direction": "debit" }
  }
]
```

`hints` opsional, dipakai hanya bila layout email belum dikenali regex.
Transaksi yang bergantung pada hint otomatis mendapat confidence lebih rendah
dan masuk antrean review.

## Menyesuaikan aturan

| Berkas | Isi |
| --- | --- |
| `config.json` | zona waktu, daftar rekening sendiri, ambang confidence, tujuan impor |
| `rules/categories.json` | kata kunci → kategori, dan pemetaan ke nama kategori Money Lover / My CashFlow+ |
| `rules/sources.json` | pengenal email per bank/dompet, regex nominal-tanggal-merchant |

### Perjalanan dinas versus liburan

Akun Money Lover ini punya dua pohon perjalanan, `Business trip` dan
`Vacation trip`, sedangkan email pemesanan hotel atau tiket tidak memuat apa
pun yang membedakan keduanya. Defaultnya karena itu `Vacation trip`.

Penandanya ditulis manual. Selama teks transaksi memuat "perjalanan dinas"
(juga "sppd", "business trip", "dinas luar"), blok `context_overrides` di
`rules/categories.json` memindahkan kategorinya:

| Default | Dengan penanda |
| --- | --- |
| Hotel Holiday | Hotel |
| Flight Holiday | Flight |
| Train holiday | Trains |
| Bus/Travel Holiday | Bus/Travel |
| F&B, Café, Jajan | Meals Biz Trip |
| Vacation trip | Business trip |

Untuk transaksi yang sudah terlanjur masuk staging tanpa penanda, gunakan
`cli tag`; kategorinya dihitung ulang tanpa mengubah fingerprint, sehingga
deduplikasi lintas run tetap bekerja.

Isi `own_accounts[].last4` dan `aliases` supaya transfer antar rekening sendiri
terdeteksi. Selama masih kosong, transfer keluar akan tercatat sebagai
pengeluaran.

### Satu dompet per rekening

Money Lover memegang beberapa dompet, dan tiap transaksi harus mendarat di
dompet rekening asalnya. Penentuannya dari penerbit email: `own_accounts[].bank`
dicocokkan ke label penerbit di `rules/sources.json`, lalu
`own_accounts[].moneylover_wallet` menentukan dompet tujuannya.

| Penerbit email | Dompet Money Lover |
| --- | --- |
| BNI | Rekening BNI IP |
| Bank Mandiri | Rekening Mandiri |
| Bank Jago | Bank Jago |
| Superbank | Superbank |
| GoPay | Gopai |

Yang dipetakan hanya dompet yang **Included in Total** dan belum diarsipkan.
Kartu kredit dan pos investasi seperti `CCBNI`, `Bibit`, atau `Stock Values`
sengaja dikeluarkan dari total oleh pemiliknya; menulis transaksi ke sana akan
mengacaukan angka yang justru ingin dijaga. `cli check` membandingkan pemetaan
di config dengan penanda `exclude_total` dan `archived` dari API, lalu
menganggapnya kesalahan bila ada dompet terpetakan yang ternyata diarsipkan,
dikeluarkan dari total, atau sudah tidak ada.

Transaksi dari penerbit di luar daftar itu **tidak dikirim**. Mendaratkannya di
dompet sembarangan akan merusak saldo dua rekening sekaligus, jadi transaksinya
ditahan di staging sampai pemetaannya dilengkapi. `moneylover.default_wallet`
sengaja dibiarkan kosong untuk alasan yang sama.

Email dari merchant seperti Tokopedia tidak menyebut rekening mana yang
terdebit, sedangkan notifikasi bank untuk transaksi yang sama menyebutnya.
Ketika keduanya terdeteksi sebagai duplikat, informasi rekening diambil dari
email bank, apa pun urutan kedatangannya.

## Menjalankan tes

```bash
python3 -m unittest discover -s automation/tests -t .
```

Tes memakai contoh email di `tests/fixtures_emails.json` dan tidak menyentuh
Gmail maupun Money Lover.

## Kredensial

Semua kredensial hanya dibaca dari environment variable dan tidak pernah
ditulis ke repo, ke `automation/data/`, atau ke pesan commit. Pesan kesalahan
dari Money Lover dikutip apa adanya, tetapi email dan password tidak pernah
ikut tercetak.

## Privasi

Yang disimpan di `automation/data/` adalah tanggal, nominal, nama merchant,
kategori, empat digit terakhir rekening, dan id email. Nomor rekening penuh
dimasker oleh `normalize.mask_account`, dan isi email tidak ikut disimpan.
