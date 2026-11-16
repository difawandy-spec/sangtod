# Video URL Extractor

Tools Python untuk mengekstrak URL video (`.mp4` / `.m3u8`) dari sebuah halaman web.
Hasil ekstraksi otomatis ditulis ke file TXT (default `hasil.txt`), dan bisa memproses
banyak URL sekaligus lewat file `bulk.txt`.

## Struktur Project

```
sangtod/
├── main.py            # script utama (CLI + menu interaktif)
├── bulk.txt           # daftar URL untuk ekstrak massal
├── hasil.txt          # output URL video (dibuat otomatis)
├── downloads/         # hasil unduhan video
├── requirements.txt   # dependensi
└── readme.md
```

## Cara Kerja

Tools memakai strategi berlapis dan otomatis lanjut ke lapisan berikutnya bila
lapisan sebelumnya tidak menemukan apa-apa:

| # | Strategi | Keterangan |
|---|----------|------------|
| 1 | **WordPress REST API** | Membaca `/wp-json/wp/v2/posts?slug=...` lalu mengambil field `meta.video_url`, `trailer_url`, `embed`. Paling andal & cepat. |
| 2 | **Scan HTML/JS** | Regex URL `mp4`/`m3u8`, atribut `<source src>`, `data-*`, JSON-LD, dan escape JS (`\/`, `\u002F`). |
| 3 | **Browser (opsional)** | Playwright membuka halaman dengan Chromium headless lalu menangkap semua request bertipe media. |

> Catatan: `sangetods.net` memakai *cloaking* — halaman HTML-nya me-redirect ke situs lain
> bila diakses tanpa sesi browser yang wajar. Karena itu strategi API jadi jalur utama.
> Baris `HTTP` pada output akan menampilkan `(cloaked -> domain)` bila redirect terdeteksi.

Setiap URL video juga **diverifikasi** (request `Range: bytes=0-1023`); hanya respons
`200/206` dengan `Content-Type` media yang dianggap valid. Ini penting karena field
`meta.tracking_url` di situs tersebut sering menyimpan varian URL yang **rusak (404)**.

## Instalasi

```bash
pip install -r requirements.txt
```

Mode browser (opsional, untuk situs dengan player JS-only):

```bash
pip install playwright
playwright install chromium
```

## Pemakaian

### 0. Tanpa argumen → menu interaktif

```bash
python main.py
```

Langsung menampilkan menu pilihan (ekstrak single, bulk, download, dll).

### 1. Satu URL

```bash
python main.py "https://sangetods.net/prank-stright-nanti-entoin-aku-sambil-dicipok-ya-ayy/"
```

Output terminal:

```
[1/1] Memproses https://sangetods.net/prank-stright-nanti-entoin-aku-sambil-dicipok-ya-ayy/ ...
==============================================================================
Halaman : https://sangetods.net/prank-stright-nanti-entoin-aku-sambil-dicipok-ya-ayy/
HTTP    : 200 (cloaked -> www.google.com)
Judul   : Prank Stright | nanti entoin aku sambil dicipok ya ayy
Durasi  : 155 detik
Sumber  : wp-rest-api, verified
Video   : 1 URL valid
  1. https://cdn.shortyearn.com/sangetods.net/videos/2026/week03/processed_ncrotu9-8b61e284-8289-455b-936e-dc59902d437a.mp4

[i] 1 URL video ditulis ke: hasil.txt
```

`hasil.txt`:

```
# ==========================================================================
# Hasil ekstraksi URL video
# Waktu   : 2026-11-16 19:08:12
# Halaman : 1 diproses, 1 berhasil
# Video   : 1 URL unik
# ==========================================================================
https://cdn.shortyearn.com/sangetods.net/videos/2026/week03/processed_ncrotu9-8b61e284-8289-455b-936e-dc59902d437a.mp4
```

### 2. Bulk via `bulk.txt`

Isi `bulk.txt` (satu URL per baris, `#` untuk komentar):

```
https://sangetods.net/prank-stright-nanti-entoin-aku-sambil-dicipok-ya-ayy/
https://sangetods.net/yaochi-03/
https://sangetods.net/webber-01-zack/
```

Jalankan:

```bash
python main.py --bulk            # otomatis membaca bulk.txt
python main.py --bulk lain.txt   # atau file lain
```

Semua URL yang ditemukan dikumpulkan ke `hasil.txt` (duplikat dibuang otomatis).

### 3. Beberapa URL di command line

```bash
python main.py "URL_1" "URL_2" "URL_3"
```

## Opsi

| Opsi | Fungsi |
|------|--------|
| `--menu` / `-m` | **Buka menu interaktif** |
| `--bulk [FILE]` / `-f` | Ekstrak massal dari file (default `bulk.txt`) |
| `--txt FILE` / `-t` | File TXT hasil (default `hasil.txt`) |
| `--no-txt` | Jangan tulis file TXT |
| `--txt-detail` | TXT dikelompokkan per halaman (bukan hanya daftar URL) |
| `--append` | Tambahkan ke TXT, jangan timpa |
| `--json` / `-j` | Output JSON ke stdout |
| `--output FILE` / `-o` | Simpan hasil JSON ke file |
| `--download` / `-d` | Unduh video yang ditemukan ke `downloads/` |
| `--download-txt [FILE]` | Unduh semua URL dari file TXT (default `hasil.txt`) |
| `--outdir DIR` | Folder unduhan (default `downloads`) |
| `--browser` / `-b` | Paksa pakai Playwright |
| `--no-verify` | Lewati verifikasi URL (lebih cepat) |
| `--referer URL` | Header `Referer` kustom |
| `--delay DETIK` | Jeda antar URL (default 1.0) |

## Menu Interaktif

```bash
python main.py --menu
```

```
+==============================================================+
|            VIDEO URL EXTRACTOR  -  MENU UTAMA                |
+==============================================================+
|  1. Ekstrak 1 URL                                            |
|  2. Ekstrak banyak URL dari file (bulk.txt)                  |
|  3. Download semua video dari hasil.txt                      |
|  4. Ekstrak 1 URL lalu langsung download                     |
|  5. Lihat isi hasil.txt                                      |
|  6. Lihat isi folder downloads                               |
|  7. Bersihkan hasil.txt                                      |
|  0. Keluar                                                   |
+==============================================================+
```

Semua hasil unduhan otomatis masuk ke folder `downloads/`.

## Download

Fitur unduh punya progress bar, resume otomatis (`.part` + `Range`), retry 3x,
dan nama file dirapikan (spasi `%20` di-decode, karakter aneh diganti `_`).
File yang namanya bentrok diberi suffix `_1`, `_2`, dst.

Tiga cara memakai:

```bash
# 1. Ekstrak lalu unduh sekaligus
python main.py "URL" --download

# 2. Unduh dari hasil.txt (hasil ekstraksi sebelumnya)
python main.py --download-txt

# 3. Bulk + unduh
python main.py --bulk --download
```

Lokasi file: `downloads/` (ubah dengan `--outdir`).

### Contoh lanjutan

Bulk + unduh semua video:

```bash
python main.py --bulk --download
```

Simpan TXT terpisah per halaman:

```bash
python main.py --bulk --txt-detail -t hasil_detail.txt
```

Ambil hanya daftar URL untuk dipipe ke `wget`:

```bash
grep -v '^#' hasil.txt | wget -i -
```

## Format `hasil.txt`

Default — hanya daftar URL (mudah dikonsumsi tool lain):

```
# header komentar ...
https://cdn.../video1.mp4
https://cdn.../video2.mp4
```

Dengan `--txt-detail` — dikelompokkan per halaman:

```
# [https://sangetods.net/yaochi-03/]
https://cdn.../video1.mp4

# [https://sangetods.net/webber-01-zack/]
https://cdn.../video2.mp4
```

## Exit Code

- `0` — minimal satu URL berhasil diekstrak
- `1` — tidak ada video yang ditemukan
- `2` — argumen / file bulk tidak valid

## Disclaimer

Gunakan hanya untuk konten yang Anda berhak akses/unduh. Hormati Terms of Service
situs dan hak cipta pemilik konten.
