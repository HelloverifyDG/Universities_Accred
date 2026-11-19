#!/usr/bin/env python3
"""
Search an institution name in the AICTE approved-institutions PDF,
highlight it, and save a screenshot as proof.

Install:  pip install pymupdf requests
Usage:    python aicte_verify.py "APJ ABDUL KALAM TECHNOLOGICAL UNIVERSITY"
          python aicte_verify.py "AMAL JYOTHI COLLEGE OF ENGINEERING" -o out --full-page
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF
import requests

PDF_URL = ("https://aicte.gov.in/sites/default/files/approval/2025-26/"
           "Groupwise-List%20of%20Institutions_compressed.pdf")
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def download_pdf(url: str, dest: Path, force: bool = False, verify_ssl: bool = True) -> Path:
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


def find_matches(doc: fitz.Document, name: str):
    """Return list of (page_index, [rects]). Tries full name, then a shorter prefix
    (names wrapped over two lines can sometimes fail a full-string match)."""
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


def capture(doc, page_index, rects, out_path: Path, zoom=2.5, full_page=False, pad=120):
    page = doc[page_index]
    for r in rects:
        annot = page.add_highlight_annot(r)
        annot.set_colors(stroke=(0.6, 0.8, 0.0))  # yellow-green like the browser
        annot.update()

    if full_page:
        clip = page.rect
    else:  # crop a band around the match, full width of the page
        top = min(r.y0 for r in rects) - pad
        bottom = max(r.y1 for r in rects) + pad
        clip = fitz.Rect(page.rect.x0, max(page.rect.y0, top),
                         page.rect.x1, min(page.rect.y1, bottom))

    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
    pix.save(out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="Institution name to search")
    ap.add_argument("-o", "--outdir", default="screenshots")
    ap.add_argument("--url", default=PDF_URL)
    ap.add_argument("--cache", default="aicte_list.pdf")
    ap.add_argument("--full-page", action="store_true", help="Capture the whole page")
    ap.add_argument("--refresh", action="store_true", help="Re-download the PDF")
    ap.add_argument("--no-verify-ssl", action="store_true")
    args = ap.parse_args()

    pdf_path = download_pdf(args.url, Path(args.cache), args.refresh, not args.no_verify_ssl)
    doc = fitz.open(pdf_path)

    used_query, matches = find_matches(doc, args.name)
    if not matches:
        print(f"NOT FOUND: '{args.name}' does not appear in the PDF.")
        sys.exit(1)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", args.name).strip("_")[:60]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for page_index, rects in matches:
        out = outdir / f"{slug}_p{page_index + 1}_{stamp}.png"
        capture(doc, page_index, rects, out, full_page=args.full_page)
        print(f"FOUND on page {page_index + 1} ({len(rects)} hit(s)) -> {out}")


if __name__ == "__main__":
    main()