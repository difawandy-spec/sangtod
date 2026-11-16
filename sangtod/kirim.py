import os
import asyncio
import subprocess
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import DocumentAttributeVideo
from pymediainfo import MediaInfo
from tqdm import tqdm

# ======================
# TELEGRAM API
# ======================

api_id = 1916950
api_hash = "9e268fee501ad809e4f5f598adcb970c"

client = TelegramClient("mysession", api_id, api_hash)

# ======================
# FILE EXTENSIONS
# ======================

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

# ======================
# GET MEDIA FILES
# ======================

def get_media_files(folder):
    media = []
    for root, dirs, files in os.walk(folder):
        for file in files:
            if file.lower().endswith(VIDEO_EXTENSIONS + IMAGE_EXTENSIONS):
                media.append(os.path.join(root, file))
    media.sort()
    return media

# ======================
# GENERATE CAPTION
# ======================

def generate_caption(file_path):
    filename = os.path.basename(file_path)
    caption = os.path.splitext(filename)[0]
    caption = caption.replace(".", " ").replace("_", " ").title()
    return caption

# ======================
# GET VIDEO METADATA
# ======================

def get_video_metadata(file_path):
    media_info = MediaInfo.parse(file_path)
    for track in media_info.tracks:
        if track.track_type == "Video":
            duration = int(track.duration / 1000) if track.duration else 0
            width = track.width if track.width else 0
            height = track.height if track.height else 0
            return duration, width, height
    return 0, 0, 0

# ======================
# GENERATE THUMBNAIL
# ======================

def generate_thumbnail(video_path):
    thumb_path = video_path + ".jpg"
    command = [
        "ffmpeg",
        "-y",
        "-ss", "00:00:05",
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        thumb_path
    ]
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return thumb_path

# ======================
# UPLOAD FILE
# ======================

async def upload_file(target, file_path, index, total):
    filename = os.path.basename(file_path)
    caption = generate_caption(file_path)
    file_size = os.path.getsize(file_path)
    
    print(f"\nUploading ({index}/{total})")
    print(filename)
    
    is_video = file_path.lower().endswith(VIDEO_EXTENSIONS)
    thumb = None
    duration, width, height = 0, 0, 0
    
    # 💡 Pindah ke luar loop: Ambil metadata & buat thumbnail sebelum retry loop
    # Agar jika gagal/retry, tidak perlu menjalankan ffmpeg ulang
    if is_video:
        duration, width, height = get_video_metadata(file_path)
        thumb = generate_thumbnail(file_path)

    with tqdm(
        total=file_size,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc=filename
    ) as pbar:

        async def progress(current, total):
            pbar.total = total
            pbar.update(current - pbar.n)

        retry = 3
        success = False
        while retry > 0:
            try:
                # =====================
                # VIDEO
                # =====================
                if is_video:
                    await client.send_file(
                        target,
                        file_path,
                        caption=caption,
                        thumb=thumb,
                        supports_streaming=True,
                        attributes=[
                            DocumentAttributeVideo(
                                duration=duration,
                                w=width,
                                h=height,
                                supports_streaming=True
                            )
                        ],
                        workers=8,
                        progress_callback=progress
                    )

                # =====================
                # IMAGE
                # =====================
                else:
                    await client.send_file(
                        target,
                        file_path,
                        caption=caption,
                        workers=4,
                        progress_callback=progress
                    )

                print("✅ Upload selesai")
                success = True
                break

            except FloodWaitError as e:
                wait = int(e.seconds)
                print(f"\n⚠ FloodWait {wait} detik. Menunggu...")
                await asyncio.sleep(wait)

            except Exception as e:
                retry -= 1
                print(f"\n❌ Error upload: {e}")
                if retry > 0:
                    print("🔁 Retry upload dalam 5 detik...")
                    await asyncio.sleep(5)
                else:
                    print("❌ Upload gagal")
                    success = False
                    
    # 💡 Pembersihan Thumbnail: Menghapus file thumbnail setelah upload selesai atau gagal
    if thumb and os.path.exists(thumb):
        try:
            os.remove(thumb)
            print("🗑️ Thumbnail sementara berhasil dihapus")
        except Exception as e:
            print(f"⚠ Gagal menghapus thumbnail: {e}")

    return success

# ======================
# RANGE PARSER
# ======================

def parse_range(range_str, total):
    """
    Terima input seperti:
    - "50"      -> ambil 50 pertama
    - "1-50"    -> ambil index 1 s/d 50
    - "all"     -> ambil semua
    Return (start_idx, end_idx) 0-based, end exclusive.
    """

    range_str = range_str.strip().lower()

    if range_str == "" or range_str == "all":
        return 0, total

    if "-" in range_str:
        try:
            start, end = range_str.split("-")
            start = int(start)
            end = int(end)
        except ValueError:
            print("⚠️ Format range salah, pakai semua list")
            return 0, total

        start_idx = max(0, start - 1)
        end_idx = min(total, end)
        return start_idx, end_idx

    try:
        n = int(range_str)
        return 0, min(total, n)
    except ValueError:
        print("⚠️ Format salah, pakai semua list")
        return 0, total

# ======================
# MAIN MENU
# ======================

async def main():
    print("\n=== TELEGRAM MEDIA UPLOADER ===\n")
    print("1. Cowok")
    print("2. Cewek")
    print("3. Saved Messages")
    print("4. Custom Target\n")

    choice = input("Pilih tujuan (1-4): ").strip()

    if choice == "1":
        target_input = "https://t.me/+2PJgDSCohYplNmY1"
    elif choice == "2":
        target_input = "https://t.me/+-b0j641W63gzNDVl"
    elif choice == "3":
        target_input = "me"
    elif choice == "4":
        target_input = input("Masukkan username / link / ID: ")
    else:
        print("❌ Pilihan tidak valid")
        return

    print("\nResolving target...")
    target = await client.get_entity(target_input)
    print("✅ Target ditemukan")

    folder = input("\nMasukkan path folder media: ")

    if not os.path.isdir(folder):
        print("❌ Folder tidak ditemukan")
        return

    media_files = get_media_files(folder)

    if not media_files:
        print("❌ Tidak ada media ditemukan")
        return

    total = len(media_files)
    print(f"\nTotal media ditemukan: {total}")

    range_str = input("Mau upload berapa? (contoh: 50 / 1-50 / all): ")

    start_idx, end_idx = parse_range(range_str, total)

    selected = media_files[start_idx:end_idx]

    print(f"\n▶️ Akan upload {len(selected)} file (index {start_idx + 1}-{end_idx})")

    success_count = 0
    fail_count = 0

    for i, media in enumerate(selected, start=1):
        ok = await upload_file(target, media, i, len(selected))

        if ok:
            success_count += 1
            try:
                os.remove(media)
                print(f"🗑️ File dihapus: {os.path.basename(media)}")
            except Exception as e:
                print(f"⚠ Gagal menghapus file {media}: {e}")
        else:
            fail_count += 1
            print(f"❌ File tetap ada (gagal upload): {os.path.basename(media)}")

    print(f"\n🎉 Selesai. {success_count} berhasil & terhapus, {fail_count} gagal & tetap ada di folder.")

# ======================
# START CLIENT
# ======================

with client:
    client.loop.run_until_complete(main())