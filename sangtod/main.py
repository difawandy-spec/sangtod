#!/usr/bin/env python3
"""
main.py — Ekstrak URL video (mp4 / m3u8) dari sebuah halaman web.

Strategi berlapis (fallback otomatis):
  1. WordPress REST API   -> baca meta.video_url / embed / trailer (paling andal)
  2. Scan HTML + JS       -> regex mp4/m3u8, og:video, JSON-LD, <source>, data-*
  3. Mode browser         -> Playwright menangkap request media (butuh instalasi)

Contoh pemakaian:
    python main.py "https://contoh.com/judul-post/"
    python main.py URL1 URL2
    python main.py URL --browser
    python main.py --bulk bulk.txt          # ekstrak banyak URL dari file
    python main.py --bulk bulk.txt -t hasil.txt
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import unquote, urljoin, urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Dependensi belum ada. Jalankan: pip install -r requirements.txt")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Ekstensi media yang dianggap "video"
MEDIA_EXTS = ("mp4", "m3u8", "webm", "mkv", "m4v", "mov")

# URL absolut menuju file video
ABS_MEDIA_RE = re.compile(
    r"""https?://[^\s"'<>\\]+?\.(?:""" + "|".join(MEDIA_EXTS) + r""")(?:\?[^\s"'<>\\]*)?""",
    re.IGNORECASE,
)

# Path relatif / absolut di dalam atribut (src, data-src, file, dsb.)
REL_MEDIA_RE = re.compile(
    r"""["']([^"'<>\s]+?\.(?:""" + "|".join(MEDIA_EXTS) + r""")(?:\?[^"'<>\s]*)?)["']""",
    re.IGNORECASE,
)

# Key JSON yang biasanya menyimpan URL video
VIDEO_META_KEYS = (
    "video_url",
    "videoUrl",
    "video",
    "embed",
    "embed_url",
    "trailer_url",
    "trailerUrl",
    "stream",
    "stream_url",
    "file",
    "src",
    "url",
    "source",
    "hls",
    "m3u8",
)

# Key yang sering menyimpan URL rusak / bukan sumber utama
SKIP_KEYS = {"tracking_url", "thumb", "thumbnail", "poster", "image", "preview"}

# Nama file default
DEFAULT_BULK_FILE = "bulk.txt"
DEFAULT_TXT_FILE = "hasil.txt"
DEFAULT_DOWNLOAD_DIR = "downloads"


# --------------------------------------------------------------------------- #
# Utilitas
# --------------------------------------------------------------------------- #
def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
            "Connection": "keep-alive",
        }
    )
    return s


def unescape_text(text: str) -> str:
    """Normalisasi escape yang umum di HTML/JS (\\/ , \\u002F , &amp;)."""
    text = text.replace("\\/", "/")
    text = re.sub(r"\\u002[fF]", "/", text)
    text = re.sub(r"\\u0026", "&", text)
    text = text.replace("&amp;", "&").replace("&#038;", "&")
    return text


def is_media_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith("." + ext) for ext in MEDIA_EXTS)


def dedupe(urls):
    """Buang duplikat; anggap sama bila path-nya identik setelah di-decode."""
    seen, out = set(), []
    for u in urls:
        if not u:
            continue
        key = unquote(urlparse(u).path) + "?" + urlparse(u).query
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def verify_url(session: requests.Session, url: str, timeout: int = 15) -> tuple[bool, str]:
    """Cek apakah URL benar-benar menyajikan media. Return (ok, keterangan)."""
    try:
        resp = session.get(
            url, headers={"Range": "bytes=0-1023"}, stream=True, timeout=timeout
        )
        ctype = resp.headers.get("Content-Type", "").split(";")[0]
        status = resp.status_code
        resp.close()
        if status in (200, 206) and ctype.startswith(("video/", "audio/", "application/octet-stream", "application/vnd.apple.mpegurl", "application/x-mpegurl")):
            return True, f"{status} {ctype}"
        return False, f"{status} {ctype or 'unknown'}"
    except requests.RequestException as exc:
        return False, f"error: {exc}"


def site_root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def slug_from_url(url: str) -> str | None:
    path = urlparse(url).path.strip("/")
    if not path:
        return None
    return path.split("/")[-1]


# --------------------------------------------------------------------------- #
# Strategi 1: scan HTML / JS
# --------------------------------------------------------------------------- #
def extract_from_html(html: str, base_url: str) -> list[str]:
    """Cari semua kandidat URL video di HTML/JS."""
    if not html:
        return []

    text = unescape_text(html)
    found: list[str] = []

    for m in ABS_MEDIA_RE.finditer(text):
        found.append(m.group(0))

    for m in REL_MEDIA_RE.finditer(text):
        candidate = m.group(1)
        if candidate.startswith("//"):
            found.append(urlparse(base_url).scheme + ":" + candidate)
        elif candidate.startswith("/") or not urlparse(candidate).scheme:
            found.append(urljoin(base_url, candidate))
        else:
            found.append(candidate)

    # filter: buang URL yang jelas bukan file media setelah urljoin
    found = [u for u in found if is_media_url(u)]
    return dedupe(found)


# --------------------------------------------------------------------------- #
# Strategi 2: WordPress REST API
# --------------------------------------------------------------------------- #
def _walk_json(obj, out: list[str]) -> None:
    """Telusuri struktur JSON, kumpulkan semua string yang berupa URL video."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in SKIP_KEYS:
                continue
            if isinstance(v, str) and k in VIDEO_META_KEYS and is_media_url(v):
                out.append(v)
            _walk_json(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_json(item, out)


def extract_from_wordpress(session: requests.Session, page_url: str) -> dict:
    """Ambil data post dari WP REST API berdasarkan slug / ID."""
    info: dict = {"ok": False, "videos": [], "meta": {}}
    root = site_root(page_url)
    parsed = urlparse(page_url)
    slug = slug_from_url(page_url)

    params = {
        "slug": slug,
        "_fields": "id,slug,link,title,date,meta,content,excerpt",
    }

    # dukung URL bentuk ?p=123
    qs_id = None
    for part in parsed.query.split("&"):
        if part.startswith("p=") and part[2:].isdigit():
            qs_id = part[2:]

    endpoints = []
    if qs_id:
        endpoints.append(f"{root}/wp-json/wp/v2/posts/{qs_id}")
    endpoints.append(f"{root}/wp-json/wp/v2/posts")

    for api in endpoints:
        try:
            resp = session.get(
                api, params=None if qs_id else params, timeout=20, allow_redirects=True
            )
            if resp.status_code != 200 or "json" not in resp.headers.get("Content-Type", ""):
                continue
            data = resp.json()
            post = data[0] if isinstance(data, list) else data
            if not isinstance(post, dict) or "id" not in post:
                continue

            meta = post.get("meta") or {}
            info["meta"] = {
                "id": post.get("id"),
                "slug": post.get("slug"),
                "title": (post.get("title") or {}).get("rendered"),
                "date": post.get("date"),
                "duration": meta.get("duration"),
                "thumb": meta.get("thumb"),
                "embed": meta.get("embed"),
                "trailer_url": meta.get("trailer_url"),
                "source": "wp-rest-api",
            }

            videos: list[str] = []
            # prioritas: video_url dulu, lalu embed/trailer; tracking_url dilewati
            for key in ("video_url", "embed", "trailer_url"):
                val = meta.get(key)
                if isinstance(val, str) and is_media_url(val):
                    videos.append(val)

            _walk_json(meta, videos)
            content_html = (post.get("content") or {}).get("rendered", "")
            videos.extend(extract_from_html(content_html, page_url))

            info["videos"] = dedupe(videos)
            info["ok"] = True
            return info
        except (requests.RequestException, ValueError):
            continue

    return info


# --------------------------------------------------------------------------- #
# Strategi 3: browser (opsional, Playwright)
# --------------------------------------------------------------------------- #
def extract_with_browser(page_url: str, timeout: int = 45) -> list[str]:
    """Buka halaman dengan Chromium headless dan tangkap request media."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "  [!] Playwright belum terpasang. "
            "Jalankan: pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return []

    captured: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
        )
        page = context.new_page()

        def on_request(req):
            url = req.url
            if is_media_url(url) or req.resource_type == "media":
                captured.append(url)

        page.on("request", on_request)
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(6000)  # beri waktu player memuat manifest
            html = page.content()
            captured.extend(extract_from_html(html, page_url))
        except Exception as exc:  # noqa: BLE001
            print(f"  [!] Browser error: {exc}", file=sys.stderr)
        finally:
            context.close()
            browser.close()

    return dedupe([u for u in captured if is_media_url(u)])


# --------------------------------------------------------------------------- #
# Unduh
# --------------------------------------------------------------------------- #
def safe_filename(url: str, index: int | None = None) -> str:
    """Buat nama file aman dari URL (decode %20, buang karakter aneh)."""
    raw = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    if not raw:
        raw = f"video{index if index is not None else ''}.mp4"
    name = re.sub(r"[^\w.\-]+", "_", raw).strip("._") or "video.mp4"
    if not name.lower().endswith(tuple("." + e for e in MEDIA_EXTS)):
        name += ".mp4"
    return name


def download_video(
    session: requests.Session,
    url: str,
    out_dir: str = DEFAULT_DOWNLOAD_DIR,
    index: int | None = None,
    retries: int = 3,
) -> str | None:
    """Unduh satu file media ke `out_dir` dengan progress bar & resume.

    Return path file hasil, atau None bila gagal.
    """
    os.makedirs(out_dir, exist_ok=True)

    name = safe_filename(url, index)
    dest = os.path.join(out_dir, name)

    # Hindari tabrakan nama: tambahkan suffix _1, _2, dst.
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        stem, ext = os.path.splitext(name)
        n = 1
        while os.path.exists(os.path.join(out_dir, f"{stem}_{n}{ext}")):
            n += 1
        dest = os.path.join(out_dir, f"{stem}_{n}{ext}")

    tmp = dest + ".part"
    have = os.path.getsize(tmp) if os.path.exists(tmp) else 0

    for attempt in range(1, retries + 1):
        try:
            headers = {}
            if have:
                headers["Range"] = f"bytes={have}-"

            with session.get(url, stream=True, timeout=60, headers=headers) as resp:
                if resp.status_code == 416:  # range tidak valid -> mulai ulang
                    os.remove(tmp)
                    have = 0
                    continue
                resp.raise_for_status()

                if have and resp.status_code == 200:
                    have = 0  # server tidak mendukung resume

                total = int(resp.headers.get("Content-Length", 0)) + have
                done = have
                with open(tmp, "ab" if have else "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = done * 100 // total
                            bar = "#" * (pct // 4)
                            print(
                                f"\r    [{bar:<25}] {pct:3d}%  "
                                f"{done / 1048576:7.1f}/{total / 1048576:.1f} MB",
                                end="",
                                flush=True,
                            )
                print()

            os.replace(tmp, dest)
            size = os.path.getsize(dest)
            print(f"    Selesai: {dest} ({size / 1048576:.1f} MB)")
            return dest

        except (requests.RequestException, OSError) as exc:
            print(f"\n    [!] Percobaan {attempt}/{retries} gagal: {exc}", file=sys.stderr)
            have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            if attempt < retries:
                time.sleep(2 * attempt)

    print(f"    [!] Gagal mengunduh: {url}", file=sys.stderr)
    return None


def download_urls(
    session: requests.Session,
    urls: list[str],
    out_dir: str = DEFAULT_DOWNLOAD_DIR,
) -> tuple[int, int]:
    """Unduh banyak URL. Return (berhasil, gagal)."""
    urls = dedupe(urls)
    if not urls:
        print("[!] Tidak ada URL untuk diunduh.")
        return 0, 0

    os.makedirs(out_dir, exist_ok=True)
    print(f"[i] Mengunduh {len(urls)} video ke folder: {out_dir}/")

    ok = fail = 0
    for i, url in enumerate(urls, 1):
        print(f"\n[{i}/{len(urls)}] {url}")
        if download_video(session, url, out_dir, index=i):
            ok += 1
        else:
            fail += 1

    print(f"\n[i] Unduhan selesai: {ok} berhasil, {fail} gagal")
    print(f"[i] Lokasi: {os.path.abspath(out_dir)}")
    return ok, fail


# --------------------------------------------------------------------------- #
# Orkestrasi
# --------------------------------------------------------------------------- #
def extract(
    session: requests.Session,
    page_url: str,
    use_browser: bool = False,
    verify: bool = True,
) -> dict:
    result = {
        "page": page_url,
        "ok": False,
        "sources": [],
        "meta": {},
        "videos": [],
        "invalid": [],
    }

    videos: list[str] = []

    # --- Strategi 1: WordPress API ---
    wp = extract_from_wordpress(session, page_url)
    if wp["ok"]:
        result["meta"].update(wp["meta"])
        videos.extend(wp["videos"])
        result["sources"].append("wp-rest-api")

    # --- Strategi 2: HTML langsung ---
    html_videos: list[str] = []
    try:
        resp = session.get(page_url, timeout=25, allow_redirects=True)
        result["meta"]["http_status"] = resp.status_code
        result["meta"]["final_url"] = resp.url
        if resp.status_code == 200:
            html_videos = extract_from_html(resp.text, resp.url)
            if html_videos:
                result["sources"].append("html-scan")
    except requests.RequestException as exc:
        result["meta"]["http_error"] = str(exc)

    videos.extend(html_videos)

    # --- Strategi 3: browser ---
    if use_browser or not videos:
        browser_videos = extract_with_browser(page_url)
        if browser_videos:
            videos.extend(browser_videos)
            result["sources"].append("browser")

    result["videos"] = dedupe(videos)

    # --- Verifikasi opsional: buang URL yang tidak menyajikan media ---
    if verify and result["videos"]:
        good, bad = [], []
        for vurl in result["videos"]:
            ok, note = verify_url(session, vurl)
            if ok:
                good.append(vurl)
            else:
                bad.append({"url": vurl, "reason": note})
        result["videos"] = good
        result["invalid"] = bad
        result["sources"].append("verified")

    result["ok"] = bool(result["videos"])
    return result


def print_result(res: dict) -> None:
    print("=" * 78)
    print(f"Halaman : {res['page']}")
    meta = res.get("meta", {})
    if meta.get("http_status"):
        final = meta.get("final_url", "-")
        host_page = urlparse(res["page"]).netloc
        host_final = urlparse(final).netloc if final != "-" else ""
        if host_final and host_final != host_page:
            # Halaman melakukan cloaking / redirect ke domain lain
            print(f"HTTP    : {meta['http_status']} (cloaked -> {host_final})")
        else:
            print(f"HTTP    : {meta['http_status']} -> {final}")
    if meta.get("title"):
        print(f"Judul   : {re.sub(r'<[^>]+>', '', meta['title'])}")
    if meta.get("duration"):
        print(f"Durasi  : {meta['duration']} detik")
    if res.get("sources"):
        print(f"Sumber  : {', '.join(res['sources'])}")

    if res["videos"]:
        print(f"Video   : {len(res['videos'])} URL valid")
        for i, url in enumerate(res["videos"], 1):
            print(f"  {i}. {url}")
    else:
        print("Video   : TIDAK ditemukan")
        print("          Coba mode browser: --browser")

    if res.get("invalid"):
        print(f"Ditolak : {len(res['invalid'])} URL tidak valid")
        for item in res["invalid"]:
            print(f"  - {item['url']}")
            print(f"    alasan: {item['reason']}")
    print()


# --------------------------------------------------------------------------- #
# File TXT: baca bulk & tulis hasil
# --------------------------------------------------------------------------- #
def read_url_file(path: str) -> list[str]:
    """Baca daftar URL dari file teks. Abaikan baris kosong & komentar (#)."""
    urls: list[str] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    return dedupe(urls)


def write_txt_results(
    results: list[dict], path: str, detailed: bool = False, append: bool = False
) -> int:
    """Tulis URL video hasil ekstraksi ke file TXT.

    Return jumlah URL unik yang ditulis.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    all_urls: list[str] = []
    for res in results:
        all_urls.extend(res["videos"])
    unique = dedupe(all_urls)

    ok_pages = sum(1 for r in results if r["ok"])
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    mode = "a" if append else "w"

    with open(path, mode, encoding="utf-8") as fh:
        if append:
            fh.write("\n")
        fh.write("# " + "=" * 74 + "\n")
        fh.write("# Hasil ekstraksi URL video\n")
        fh.write(f"# Waktu   : {stamp}\n")
        fh.write(f"# Halaman : {len(results)} diproses, {ok_pages} berhasil\n")
        fh.write(f"# Video   : {len(unique)} URL unik\n")
        fh.write("# " + "=" * 74 + "\n")

        if detailed:
            for res in results:
                fh.write(f"\n# [{res['page']}]\n")
                if res["videos"]:
                    for url in res["videos"]:
                        fh.write(f"{url}\n")
                else:
                    fh.write("# (tidak ada video ditemukan)\n")
        else:
            for url in unique:
                fh.write(f"{url}\n")

    return len(unique)


# --------------------------------------------------------------------------- #
# Menu interaktif
# --------------------------------------------------------------------------- #
MENU_TEXT = """
=== VIDEO URL EXTRACTOR ===

1. Single Extract
2. Bulk Extract
3. Download

4. Tools
0. Keluar
"""

TOOLS_TEXT = """
--- TOOLS ---

1. Lihat hasil.txt
2. Lihat folder downloads
3. Bersihkan hasil.txt

0. Kembali
"""


def _ask(prompt: str, default: str = "") -> str:
    """input() yang aman terhadap Ctrl+C / EOF."""
    try:
        val = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(0)
    return val or default


def _menu_extract_one(args, auto_download: bool = False) -> None:
    url = _ask("URL halaman: ")
    if not url:
        print("[!] URL kosong, dibatalkan.")
        return
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    session = build_session()
    if args.referer:
        session.headers["Referer"] = args.referer

    print(f"\n[i] Memproses {url} ...")
    res = extract(session, url, use_browser=args.browser, verify=not args.no_verify)
    print_result(res)

    if not args.no_txt:
        count = write_txt_results(
            [res], args.txt, detailed=args.txt_detail, append=args.append
        )
        print(f"[i] {count} URL video ditulis ke: {args.txt}")

    if not res["videos"]:
        return

    want = auto_download or _ask("Download sekarang? [y/N]: ").lower().startswith("y")
    if want:
        download_urls(session, res["videos"], args.outdir)


def _menu_extract_bulk(args) -> None:
    path = _ask(f"File daftar URL [{DEFAULT_BULK_FILE}]: ", DEFAULT_BULK_FILE)
    if not os.path.exists(path):
        print(f"[!] File tidak ditemukan: {path}")
        return
    urls = read_url_file(path)
    if not urls:
        print(f"[!] Tidak ada URL valid di {path}")
        return
    print(f"[i] Memuat {len(urls)} URL dari {path}\n")

    session = build_session()
    if args.referer:
        session.headers["Referer"] = args.referer

    results = []
    for idx, url in enumerate(urls, 1):
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        print(f"[{idx}/{len(urls)}] Memproses {url} ...")
        res = extract(session, url, use_browser=args.browser, verify=not args.no_verify)
        results.append(res)
        print_result(res)
        if idx < len(urls):
            time.sleep(args.delay)

    if not args.no_txt:
        count = write_txt_results(
            results, args.txt, detailed=args.txt_detail, append=args.append
        )
        print(f"[i] {count} URL video ditulis ke: {args.txt}")

    ok = sum(1 for r in results if r["ok"])
    print(f"[i] Selesai: {ok}/{len(results)} halaman berhasil")

    if _ask("Download semua video sekarang? [y/N]: ").lower().startswith("y"):
        all_urls = [u for r in results for u in r["videos"]]
        download_urls(session, all_urls, args.outdir)


def _menu_download_txt(args) -> None:
    path = _ask(f"File TXT sumber [{args.txt}]: ", args.txt)
    if not os.path.exists(path):
        print(f"[!] File tidak ditemukan: {path}")
        return
    urls = read_url_file(path)
    if not urls:
        print(f"[!] Tidak ada URL di {path}")
        return
    print(f"[i] {len(urls)} URL ditemukan di {path}")
    session = build_session()
    if args.referer:
        session.headers["Referer"] = args.referer
    download_urls(session, urls, args.outdir)


def _menu_show_txt(args) -> None:
    if not os.path.exists(args.txt):
        print(f"[!] {args.txt} belum ada. Jalankan ekstraksi dulu.")
        return
    with open(args.txt, encoding="utf-8") as fh:
        print("\n" + fh.read().rstrip() + "\n")


def _menu_show_downloads(args) -> None:
    if not os.path.isdir(args.outdir):
        print(f"[!] Folder {args.outdir}/ belum ada.")
        return
    files = sorted(
        f
        for f in os.listdir(args.outdir)
        if os.path.isfile(os.path.join(args.outdir, f))
    )
    if not files:
        print(f"[i] Folder {args.outdir}/ kosong.")
        return
    total = 0
    print(f"\nIsi folder {args.outdir}/:")
    for i, name in enumerate(files, 1):
        size = os.path.getsize(os.path.join(args.outdir, name))
        total += size
        print(f"  {i:2d}. {name}  ({size / 1048576:.1f} MB)")
    print(f"\n  Total: {len(files)} file, {total / 1048576:.1f} MB\n")


def _menu_clean_txt(args) -> None:
    if not os.path.exists(args.txt):
        print(f"[i] {args.txt} tidak ada, tidak perlu dibersihkan.")
        return
    if _ask(f"Hapus {args.txt}? [y/N]: ").lower().startswith("y"):
        os.remove(args.txt)
        print(f"[i] {args.txt} dihapus.")


def _tools_menu(args) -> None:
    """Submenu Tools: lihat hasil, lihat downloads, bersihkan."""
    actions = {
        "1": _menu_show_txt,
        "2": _menu_show_downloads,
        "3": _menu_clean_txt,
    }

    while True:
        print(TOOLS_TEXT)
        choice = _ask("Pilih [0-3]: ")

        if choice in ("0", "q"):
            return

        action = actions.get(choice)
        if not action:
            print("[!] Pilihan tidak valid.")
            continue

        print()
        try:
            action(args)
        except SystemExit:
            raise
        except KeyboardInterrupt:
            print("\n[i] Dibatalkan.")
        except Exception as exc:  # noqa: BLE001
            print(f"[!] Terjadi kesalahan: {exc}")


def interactive_menu(args) -> int:
    """Menu interaktif sederhana berbasis teks."""
    actions = {
        "1": lambda a: _menu_extract_one(a),
        "2": _menu_extract_bulk,
        "3": _menu_download_txt,
        "4": _tools_menu,
    }

    while True:
        print(MENU_TEXT)
        choice = _ask("Pilih [0-4]: ")

        if choice in ("0", "q", "exit"):
            print("Sampai jumpa!")
            return 0

        action = actions.get(choice)
        if not action:
            print("[!] Pilihan tidak valid.")
            continue

        print()
        try:
            action(args)
        except SystemExit:
            raise
        except KeyboardInterrupt:
            print("\n[i] Dibatalkan.")
        except Exception as exc:  # noqa: BLE001
            print(f"[!] Terjadi kesalahan: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ekstrak URL video (mp4/m3u8) dari halaman web.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("urls", nargs="*", help="Satu atau lebih URL halaman")
    ap.add_argument(
        "--menu", "-m",
        action="store_true",
        help="Buka menu interaktif",
    )
    ap.add_argument(
        "--bulk", "-f",
        nargs="?",
        const=DEFAULT_BULK_FILE,
        metavar="FILE",
        help=f"Ekstrak massal dari file daftar URL (default: {DEFAULT_BULK_FILE})",
    )
    ap.add_argument("--browser", "-b", action="store_true", help="Paksa pakai Playwright")
    ap.add_argument("--json", "-j", action="store_true", help="Output JSON ke stdout")
    ap.add_argument("--output", "-o", help="Simpan hasil JSON ke file")
    ap.add_argument(
        "--txt", "-t",
        default=DEFAULT_TXT_FILE,
        metavar="FILE",
        help=f"File TXT tujuan URL hasil ekstraksi (default: {DEFAULT_TXT_FILE})",
    )
    ap.add_argument("--no-txt", action="store_true", help="Jangan tulis file TXT")
    ap.add_argument(
        "--txt-detail",
        action="store_true",
        help="TXT dikelompokkan per halaman (default: daftar URL saja)",
    )
    ap.add_argument("--append", action="store_true", help="Tambahkan ke TXT, jangan timpa")
    ap.add_argument("--delay", type=float, default=1.0, help="Jeda antar URL (detik)")
    ap.add_argument(
        "--download", "-d",
        action="store_true",
        help=f"Unduh video yang ditemukan ke folder {DEFAULT_DOWNLOAD_DIR}/",
    )
    ap.add_argument(
        "--download-txt",
        nargs="?",
        const=DEFAULT_TXT_FILE,
        metavar="FILE",
        help=f"Unduh semua URL dari file TXT hasil (default: {DEFAULT_TXT_FILE})",
    )
    ap.add_argument(
        "--outdir",
        default=DEFAULT_DOWNLOAD_DIR,
        help=f"Folder unduhan (default: {DEFAULT_DOWNLOAD_DIR})",
    )
    ap.add_argument("--referer", help="Header Referer kustom untuk permintaan halaman")
    ap.add_argument(
        "--no-verify",
        action="store_true",
        help="Lewati verifikasi URL (lebih cepat, tapi URL rusak ikut tampil)",
    )
    args = ap.parse_args()

    # --- Tanpa argumen sama sekali -> buka menu interaktif ---
    if not args.menu and not args.urls and args.bulk is None and args.download_txt is None:
        return interactive_menu(args)

    # --- Menu interaktif ---
    if args.menu:
        return interactive_menu(args)

    # --- Mode unduh dari file TXT saja ---
    if args.download_txt is not None:
        session = build_session()
        if args.referer:
            session.headers["Referer"] = args.referer
        if not os.path.exists(args.download_txt):
            print(f"[!] File TXT tidak ditemukan: {args.download_txt}", file=sys.stderr)
            return 2
        urls = read_url_file(args.download_txt)
        if not urls:
            print(f"[!] Tidak ada URL di {args.download_txt}", file=sys.stderr)
            return 2
        ok, _ = download_urls(session, urls, args.outdir)
        return 0 if ok else 1

    targets = list(args.urls)

    # --- Bulk: --bulk [file] (default bulk.txt) ---
    bulk_file = args.bulk
    if bulk_file:
        if not os.path.exists(bulk_file):
            print(f"[!] File bulk tidak ditemukan: {bulk_file}", file=sys.stderr)
            return 2
        loaded = read_url_file(bulk_file)
        if not loaded:
            print(f"[!] File bulk kosong: {bulk_file}", file=sys.stderr)
            return 2
        print(f"[i] Memuat {len(loaded)} URL dari {bulk_file}")
        targets += loaded

    if not targets:
        ap.print_help()
        print("\nContoh:")
        print('  python main.py "https://sangetods.net/judul-post/"')
        print("  python main.py --bulk bulk.txt")
        print("  python main.py --bulk")
        return 2

    session = build_session()
    if args.referer:
        session.headers["Referer"] = args.referer
    results = []

    for idx, url in enumerate(targets, 1):
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if not args.json:
            print(f"[{idx}/{len(targets)}] Memproses {url} ...")
        res = extract(
            session, url, use_browser=args.browser, verify=not args.no_verify
        )
        results.append(res)
        if not args.json:
            print_result(res)

        if args.download and res["videos"]:
            for vurl in res["videos"]:
                print(f"  Mengunduh {vurl}")
                path = download_video(session, vurl, args.outdir)
                if path:
                    print(f"  Tersimpan: {path}")

        if idx < len(targets):
            time.sleep(args.delay)

    # --- Tulis URL hasil ke file TXT ---
    txt_count = 0
    if not args.no_txt:
        try:
            txt_count = write_txt_results(
                results, args.txt, detailed=args.txt_detail, append=args.append
            )
        except OSError as exc:
            print(f"[!] Gagal menulis {args.txt}: {exc}", file=sys.stderr)

    payload = results[0] if len(results) == 1 else results

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        if not args.json:
            print(f"[i] Hasil JSON disimpan ke: {args.output}")

    if not args.json and not args.no_txt:
        if txt_count:
            print(f"[i] {txt_count} URL video ditulis ke: {args.txt}")
        else:
            print(f"[i] Tidak ada URL video; {args.txt} dibuat tanpa entri")

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    return 0 if any(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
