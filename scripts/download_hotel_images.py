#!/usr/bin/env python3
"""Lädt alle Hotel-Bilder von golfbelek.com (packagePhotos) pro Hotel herunter,
optimiert sie (max 1280px breit, JPEG q82) und speichert lokal + Manifest.
Robinson Nobilis (nicht auf golfbelek) wird separat von Bilyana geholt.
"""
import io
import json
import os
from collections import defaultdict

import httpx
from PIL import Image

BASE = "/mnt/HC_Volume_104744115/golf-search"
OUT_DIR = os.path.join(BASE, "static", "hotel-images")
MANIFEST = os.path.join(BASE, "data", "hotel_images.json")
BOOTSTRAP = "https://golfbelek.com/gp/server/api/bootstrap.php"

# oid -> golfbelek packagePhotos tag
TAG_BY_OID = {
    1: "cornelia-diamond-golf-resort-spa",
    2: "cullinan-belek",
    3: "gloria-golf-resort",
    4: "gloria-serenity",
    5: "gloria-verde",
    6: "kaya-belek",
    7: "kaya-palazzo-golf-resort",
    8: "kempinski-the-dome",
    9: "lykia-world-antalya",
    10: "maxx-royal-belek-golf-resort",
    11: "regnum-carya",
    12: "regnum-the-crowm",
    # 13 Robinson Nobilis -> Bilyana (separat)
    14: "sirene-belek",
    15: "sueno-hotels-deluxe-belek",
    16: "sueno-hotels-golf-belek",
    17: "titanic-deluxe-golf-belek",
    18: "voyage-belek-golf-spa",
}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _referer_for(url: str) -> str:
    """Manche CDNs (etstur) erlauben Hotlinking nur mit eigener Referer-Domain."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    if "etstur.com" in host:
        return "https://www.etstur.com/"
    if "jollytur.com" in host:
        return "https://www.jollytur.com/"
    return "https://golfbelek.com/"


def _headers_for(url: str) -> dict:
    return {
        "User-Agent": UA,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": _referer_for(url),
    }


def fetch_photos_by_tag():
    h = {"User-Agent": "Mozilla/5.0", "Referer": "https://golfbelek.com/"}
    data = httpx.get(BOOTSTRAP, headers=h, timeout=60).json()
    by_tag = defaultdict(list)
    for p in data.get("packagePhotos", []):
        url = p.get("imageUrl")
        if not url:
            continue
        if url.startswith("/"):
            url = "https://golfbelek.com" + url
        by_tag[p.get("tag")].append(url)
    return by_tag


def optimize_and_save(raw: bytes, dest_path: str) -> bool:
    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        w, h = img.size
        if w > 1280:
            nh = int(h * (1280 / w))
            img = img.resize((1280, nh), Image.LANCZOS)
        img.save(dest_path, "JPEG", quality=82, optimize=True, progressive=True)
        return True
    except Exception as e:
        print("    optimize fail:", str(e)[:80])
        return False


def download(url: str) -> bytes | None:
    try:
        r = httpx.get(url, headers=_headers_for(url), timeout=40, follow_redirects=True)
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and len(r.content) > 2000 and ct.startswith("image"):
            return r.content
        print(f"    HTTP {r.status_code} len {len(r.content)} ct={ct[:20]} :: {url[:70]}")
    except Exception as e:
        print(f"    DL fail {str(e)[:60]} :: {url[:70]}")
    return None


# oid -> (bilyana_oid, slug) für Bilyana-Galerie (Fallback / Auffüllen)
BILYANA_GALLERY = {
    1: (65, "cornelia"), 2: (347, "cullinan"), 3: (67, "gloria-golf"),
    4: (69, "gloria-serenity"), 5: (68, "gloria-verde"), 6: (80, "kaya-belek"),
    7: (74, "kaya-palazzo"), 10: (75, "maxx-royal"), 11: (72, "regnum-carya"),
    13: (91, "robinson"), 14: (66, "sirene"), 15: (71, "sueno-deluxe"),
    16: (70, "sueno-golf"), 17: (73, "titanic"), 18: (76, "voyage"),
}


def fetch_bilyana_gallery(oid: int) -> list:
    """Holt die Bilyana-Hotel-Galerie-URLs (files/images/r_<boid>_*.jpg)."""
    import re
    info = BILYANA_GALLERY.get(oid)
    if not info:
        return []
    boid, _slug = info
    slugmap = {
        1: "cornelia-diamond-golf-resort-spa", 2: "cullinan-belek", 3: "gloria-golf-resort",
        4: "gloria-serenity-resort", 5: "gloria-verde-resort", 6: "kaya-belek-hotel",
        7: "kaya-palazzo-golf-resort", 10: "maxx-royal-belek-golf-resort", 11: "regnum-carya",
        13: "robinson-nobilis-belek", 14: "sirene-belek-hotel", 15: "sueno-hotels-deluxe-belek",
        16: "sueno-hotels-golf-belek", 17: "titanic-deluxe-golf-belek", 18: "voyage-belek-golf-spa",
    }
    page_slug = slugmap.get(oid)
    if not page_slug:
        return []
    url = f"https://bilyanagolf.com/hotel-details/turkey/belek/{page_slug}"
    try:
        r = httpx.get(url, headers={"User-Agent": UA}, timeout=40, follow_redirects=True)
        raw = re.findall(r'files/ima\w+/(?:webp/)?r_%d_[^"\']+?\.(?:jpg|jpeg|webp)' % boid, r.text, re.I)
        # bevorzuge .jpg (PIL-sicher), dedupe nach basename ohne extension, "main" zuletzt
        jpgs, seen = [], set()
        for m in raw:
            flat = m.replace('webp/', '')
            base = re.sub(r'\.(jpg|jpeg|webp)$', '', flat, flags=re.I)
            if base in seen:
                continue
            seen.add(base)
            jpgs.append("https://bilyanagolf.com/" + base + '.jpg')
        jpgs.sort(key=lambda u: ('main' in u.lower(), u))
        return jpgs
    except Exception as e:
        print("  bilyana gallery fail", str(e)[:80])
        return []


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    by_tag = fetch_photos_by_tag()
    manifest = {}
    all_oids = sorted(set(TAG_BY_OID) | set(BILYANA_GALLERY))
    for oid in all_oids:
        tag = TAG_BY_OID.get(oid)
        urls = list(by_tag.get(tag, [])) if tag else []
        hotel_dir = os.path.join(OUT_DIR, str(oid))
        os.makedirs(hotel_dir, exist_ok=True)
        saved = []
        print(f"\nHotel {oid} ({tag or 'bilyana-only'}) -> {len(urls)} golfbelek URLs")
        for url in urls:
            raw = download(url)
            if not raw:
                continue
            idx = len(saved)
            dest = os.path.join(hotel_dir, f"{idx}.jpg")
            if optimize_and_save(raw, dest):
                saved.append(f"/static/hotel-images/{oid}/{idx}.jpg")
                print(f"    saved {idx}.jpg (golfbelek)")
        # Auffüllen via Bilyana-Galerie bis mindestens 4 Bilder
        if len(saved) < 4:
            gal = fetch_bilyana_gallery(oid)
            print(f"    bilyana gallery: {len(gal)} candidates")
            for url in gal:
                if len(saved) >= 6:
                    break
                raw = download(url)
                if not raw:
                    continue
                idx = len(saved)
                dest = os.path.join(hotel_dir, f"{idx}.jpg")
                if optimize_and_save(raw, dest):
                    saved.append(f"/static/hotel-images/{oid}/{idx}.jpg")
                    print(f"    saved {idx}.jpg (bilyana)")
        manifest[str(oid)] = saved
        print(f"  => {len(saved)} images saved for hotel {oid}")

    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    with open(MANIFEST, "w") as f:
        json.dump(manifest, f, indent=1)
    print("\nManifest written to", MANIFEST)
    total = sum(len(v) for v in manifest.values())
    print("TOTAL images:", total, "across", len([k for k, v in manifest.items() if v]), "hotels")
    missing = [k for k, v in manifest.items() if not v]
    if missing:
        print("HOTELS WITH NO IMAGES:", missing)


if __name__ == "__main__":
    main()
