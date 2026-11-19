#!/usr/bin/env python3
"""
Search an institution name in the AICTE approved-institutions PDF,
highlight it, and save a screenshot (with a proof banner) as evidence.

Install:  pip install pymupdf requests pillow
Usage:    python verify_aicte.py
          python verify_aicte.py "AMAL JYOTHI COLLEGE OF ENGINEERING" -o proofs --full-page
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import pymupdf
import requests
from PIL import Image, ImageDraw, ImageFont

PDF_URL = ("https://aicte.gov.in/sites/default/files/approval/2025-26/"
           "Groupwise-List%20of%20Institutions_compressed.pdf")
DEFAULT_NAME = """Vishweshwar Education Societys Western College Of Commerce And Business Management"""
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def download_pdf(url, dest: Path, force=False, verify_ssl=True) -> Path:
    """Download once and reuse the cached copy (the PDF is large)."""
    if dest.exists() and not force:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, headers=HEADERS, stream=True, timeout=120, verify=verify_ssl) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                f.write(chunk)
    return dest


def find_matches(doc, name):
    """Return (query_used, [(page_index, [rects])]). Falls back to a shorter
    prefix if the full name fails (e.g. odd line wrapping)."""
    words = name.split()
    candidates = [name]
    if len(words) > 3:
        candidates.append(" ".join(words[:3]))
    for query in candidates:
        results = []
        for i, page in enumerate(doc):
            rects = page.search_for(query)  # case-insensitive
            if rects:
                results.append((i, rects))
        if results:
            return query, results
    return None, []


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # older Pillow
        return ImageFont.load_default()


def add_banner(img_path: Path, lines):
    img = Image.open(img_path).convert("RGB")
    font = _font(max(14, img.width // 70))
    line_h = font.size + 8 if hasattr(font, "size") else 22
    banner_h = line_h * len(lines) + 16
    canvas = Image.new("RGB", (img.width, img.height + banner_h), "white")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, img.width, banner_h], fill=(240, 240, 240))
    y = 8
    for text in lines:
        draw.text((12, y), text, fill=(20, 20, 20), font=font)
        y += line_h
    canvas.paste(img, (0, banner_h))
    canvas.save(img_path)


def capture(doc, page_index, rects, out_path: Path, zoom=2.5, full_page=False, pad=120):
    page = doc[page_index]
    for r in rects:
        annot = page.add_highlight_annot(r)
        annot.set_colors(stroke=(0.6, 0.8, 0.0))  # yellow-green like the browser
        annot.update()

    if full_page:
        clip = page.rect
    else:  # band around the match, full page width
        top = min(r.y0 for r in rects) - pad
        bottom = max(r.y1 for r in rects) + pad
        clip = pymupdf.Rect(page.rect.x0, max(page.rect.y0, top),
                            page.rect.x1, min(page.rect.y1, bottom))

    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip)
    pix.save(out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?", default=DEFAULT_NAME, help="Institution name to search")
    ap.add_argument("-o", "--outdir", default="screenshots")
    ap.add_argument("--url", default=PDF_URL)
    ap.add_argument("--cache", default="aicte_list.pdf")
    ap.add_argument("--full-page", action="store_true", help="Capture the whole page")
    ap.add_argument("--refresh", action="store_true", help="Re-download the PDF")
    ap.add_argument("--no-verify-ssl", action="store_true")
    ap.add_argument("--no-banner", action="store_true", help="Skip the proof banner")
    args = ap.parse_args()

    pdf_path = download_pdf(args.url, Path(args.cache), args.refresh, not args.no_verify_ssl)
    doc = pymupdf.open(pdf_path)

    _, matches = find_matches(doc, args.name)
    if not matches:
        print(f"NOT FOUND: '{args.name}' does not appear in the PDF.")
        sys.exit(1)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", args.name).strip("_")[:60]
    now = datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")

    for page_index, rects in matches:
        out = outdir / f"{slug}_p{page_index + 1}_{stamp}.png"
        capture(doc, page_index, rects, out, full_page=args.full_page)
        if not args.no_banner:
            add_banner(out, [
                f"Source: {args.url}",
                f"Searched: {args.name}  |  Page {page_index + 1} of {len(doc)}",
                f"Captured: {now.strftime('%d %b %Y, %H:%M:%S')}",
            ])
        print(f"FOUND on page {page_index + 1} "
              f"(1 match, {len(rects)} line fragment(s)) -> {out}")


if __name__ == "__main__":
    main()