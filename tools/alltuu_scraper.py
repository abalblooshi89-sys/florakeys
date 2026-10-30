"""
Download every photo in an Alltuu (喔图) live album at original resolution.

The album page loads its photos from a JSON endpoint (/rest/v4c/fplN/...).
Each entry carries signed OSS links in several sizes:
    sl  ~720px display image
    bl  1600px preview
    ol  original upload (full resolution, original filename)
The links are signed (Expires / OSSAccessKeyId / Signature), so the query
string must be kept intact - stripping it returns 403.

This script drives the page with Playwright, captures those JSON responses
while scrolling, then downloads the chosen size in parallel.

Usage:
    pip install playwright requests tqdm && playwright install chromium
    python alltuu_scraper.py "<album url>" [-o folder] [--size ol|bl|sl] [--workers 8]
"""
import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote, urlparse

import requests
from playwright.sync_api import sync_playwright
from tqdm import tqdm

DEFAULT_ALBUM_URL = "https://m.alltuu.com/album/676a6ed4bd646a242f32abed2f0bc5f2/1000000033?from=link&menu=live"
DEFAULT_OUTPUT_DIR = "alltuu_photos"
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)
PHOTO_LIST_MARKER = "/rest/v4c/fplN/"


def filename_for(photo: dict, url: str) -> str:
    """Use the original filename embedded in the signed link, else the photo id."""
    match = re.search(r'filename%3D%22(.+?)%22', url) or re.search(r'filename="(.+?)"', unquote(url))
    if match:
        name = unquote(match.group(1))
    else:
        ext = os.path.splitext(urlparse(url).path)[1] or ".jpg"
        name = f"{photo['id']}{ext}"
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def collect_photos(album_url: str, headless: bool, idle_rounds: int) -> dict:
    """Scroll the album and capture photo-list API responses. Returns {photo_id: photo}."""
    photos = {}

    def on_response(response):
        if PHOTO_LIST_MARKER not in response.url:
            return
        try:
            for photo in response.json().get("d") or []:
                if isinstance(photo, dict) and photo.get("id"):
                    photos[photo["id"]] = photo
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=MOBILE_UA, viewport={"width": 390, "height": 844})
        page = context.new_page()
        page.on("response", on_response)

        print(f"Loading album: {album_url}")
        page.goto(album_url, wait_until="networkidle", timeout=60000)
        time.sleep(3)

        print("Scrolling album to load every page of photos...")
        last_count = 0
        idle = 0
        while idle < idle_rounds:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.mouse.wheel(0, 2000)
            time.sleep(1.5)

            if len(photos) == last_count:
                idle += 1
            else:
                idle = 0
                last_count = len(photos)
                print(f"  {last_count} photos found")

        browser.close()

    return photos


def download_one(session: requests.Session, url: str, path: str, retries: int = 4) -> str:
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return "skipped"
    tmp = path + ".part"
    for attempt in range(retries):
        try:
            with session.get(url, timeout=60, stream=True) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(1 << 16):
                        f.write(chunk)
            os.replace(tmp, path)
            return "downloaded"
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def download_photos(photos: dict, output_dir: str, size: str, workers: int):
    os.makedirs(output_dir, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": MOBILE_UA, "Referer": "https://m.alltuu.com/"})

    jobs = []
    for photo in sorted(photos.values(), key=lambda ph: ph.get("time") or 0):
        url = photo.get(size) or photo.get("ol") or photo.get("bl") or photo.get("sl")
        if url:
            jobs.append((url, os.path.join(output_dir, filename_for(photo, url))))

    with open(os.path.join(output_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(list(photos.values()), f, ensure_ascii=False, indent=1)

    print(f"\nDownloading {len(jobs)} photos ({size}) into '{output_dir}'...")
    failed = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download_one, session, url, path): path for url, path in jobs}
        for future in tqdm(as_completed(futures), total=len(futures), desc="Progress"):
            try:
                future.result()
            except Exception as e:
                failed.append((futures[future], str(e)))

    for path, err in failed:
        print(f"Failed: {os.path.basename(path)}: {err}")
    print(f"\nDone: {len(jobs) - len(failed)} ok, {len(failed)} failed. Re-run to retry failures.")


def main():
    parser = argparse.ArgumentParser(description="Download an Alltuu album at original resolution.")
    parser.add_argument("url", nargs="?", default=DEFAULT_ALBUM_URL)
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--size", choices=["ol", "bl", "sl"], default="ol",
                        help="ol = original (default), bl = 1600px, sl = ~720px")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--idle-rounds", type=int, default=6,
                        help="stop scrolling after this many scrolls with no new photos")
    parser.add_argument("--limit", type=int, default=0, help="only download the first N photos (testing)")
    parser.add_argument("--show-browser", action="store_true")
    args = parser.parse_args()

    photos = collect_photos(args.url, headless=not args.show_browser, idle_rounds=args.idle_rounds)
    if not photos:
        print("No photos were captured. Check the URL or try --show-browser.")
        return
    if args.limit:
        photos = dict(list(photos.items())[: args.limit])
    download_photos(photos, args.output, args.size, args.workers)


if __name__ == "__main__":
    main()
