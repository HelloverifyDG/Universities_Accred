#!/usr/bin/env python3
"""
Capture one proof screenshot and store it under the university name.

    screenshots/ugc/Anjaneya_University__Chhattisgarh.png
    screenshots/aicte/Anjaneya_University__Chhattisgarh.png

Only PNG files are written. Pass the university name and the database
decides the list: UGC, AICTE, or both. UGC is captured first when the
university is on both lists.

    python pipeline.py --name "Anjaneya University"
    python pipeline.py --name "Amity University" --state "Rajasthan"

When several institutions share a name, pass --state (and --city if needed).
"""
import argparse
import importlib.util
import os
import re
from datetime import datetime
from pathlib import Path

import pymupdf
import pymysql
from pymysql.cursors import DictCursor

ROOT = Path(__file__).resolve().parent
DEMO = ROOT.parent
SHOTS = ROOT / "screenshots"
PDF_PATH = DEMO / "aicte_verification" / "aicte_list.pdf"
UGC_URL = "https://www.ugc.gov.in/universitydetails/university?type=ddmCMsxJZgXH2S/m0uMOKQ=="
AICTE_PDF_URL = (
    "https://aicte.gov.in/sites/default/files/approval/2025-26/"
    "Groupwise-List%20of%20Institutions_compressed.pdf"
)
SOURCES = ("ugc", "aicte")
STATE_ALIASES = {
    "orissa": "odisha",
    "pondicherry": "puducherry",
    "uttaranchal": "uttarakhand",
    "nctofdelhi": "delhi",
}


class AmbiguousName(LookupError):
    """Several institutions share this name. The caller must narrow it."""

    def __init__(self, message, matches):
        super().__init__(message)
        self.matches = matches


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def connect():
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "root"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "university_registry"),
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
    )


def name_key(value):
    text = (value or "").upper().replace("&", " AND ")
    return re.sub(r"[^A-Z0-9]", "", text)


def state_key(value):
    key = re.sub(r"[^a-z]", "", (value or "").lower())
    return STATE_ALIASES.get(key, key)


def file_slug(name, state):
    """Filesystem tag built from the university name and its state."""
    def piece(value):
        return re.sub(r"[^A-Za-z0-9]+", "_", value or "").strip("_")

    slug = piece(name)[:80] or "university"
    if state:
        slug = f"{slug}__{piece(state)[:40]}"
    return slug


def check_source(source):
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")


def png_path(source, name, state):
    check_source(source)
    return SHOTS / source / f"{file_slug(name, state)}.png"


def _choices(rows):
    picked = []
    seen = set()
    for row in rows:
        key = (row["name"], row["state"] or "", row["city"] or "")
        if key in seen:
            continue
        seen.add(key)
        picked.append({"name": row["name"], "state": row["state"], "city": row["city"]})
        if len(picked) == 20:
            break
    return picked


def applicable_sources(row):
    """UGC first when the university is on both lists, otherwise the list it is on."""
    sources = []
    if row["in_ugc"]:
        sources.append("ugc")
    if row["in_aicte"]:
        sources.append("aicte")
    if not sources:
        raise LookupError(f"{row['name']} is not on the UGC or AICTE list")
    return sources


def fetch_institution(name, state=None, city=None):
    """Resolve one institution from its university name."""
    key = name_key(name)
    if not key:
        raise ValueError("university name is required")
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM institutions WHERE name_key = %s", (key,))
            rows = cursor.fetchall()
    finally:
        connection.close()

    if state:
        wanted = state_key(state)
        rows = [row for row in rows if state_key(row["state"]) == wanted]
    if city:
        wanted_city = name_key(city)
        rows = [row for row in rows if name_key(row["city"]) == wanted_city]
    if not rows:
        where = name
        if state:
            where += f", {state}"
        raise LookupError(f"no institution named {where}")

    states = {row["state"] or "" for row in rows}
    if state is None and len(states) > 1:
        raise AmbiguousName(
            f"More than one institution is named {name}. Pass state.",
            _choices(rows),
        )
    cities = {row["city"] or "" for row in rows if row["city"]}
    if city is None and len(cities) > 1:
        raise AmbiguousName(
            f"More than one institution named {name} is in that state. Pass city.",
            _choices(rows),
        )
    rows.sort(key=lambda row: (0 if row["in_ugc"] else 1, row["aicte_sr_no"] or 10**9, row["id"]))
    return rows[0]


def find_aicte_page(permanent_id):
    """Return (page_index, rects, doc) for the institution's row in the AICTE PDF."""
    if not PDF_PATH.exists():
        raise FileNotFoundError(f"AICTE PDF not found: {PDF_PATH}")
    doc = pymupdf.open(PDF_PATH)
    for index, page in enumerate(doc):
        hits = []
        for rect in page.search_for(permanent_id):
            words = page.get_text("words", clip=rect + pymupdf.Rect(-1, -2, 20, 2))
            if any(word[4] == permanent_id for word in words):
                hits.append(rect)
        if hits:
            return index, hits, doc
    return None, [], doc


def capture_aicte(row, out: Path):
    permanent_id = row["aicte_permanent_id"]
    if not permanent_id:
        raise LookupError(f"{row['name']} has no AICTE permanent id")
    page_index, rects, doc = find_aicte_page(permanent_id)
    if page_index is None:
        raise LookupError(f"{row['name']} was not found in the AICTE PDF")

    verifier = load_module("aicte_verify", DEMO / "aicte_verification" / "main.py")
    out.parent.mkdir(parents=True, exist_ok=True)
    verifier.capture(doc, page_index, rects, out, full_page=False)
    verifier.add_banner(out, [
        f"Source: {AICTE_PDF_URL}",
        f"University: {row['name']}   |   {row['state'] or ''}   |   Page {page_index + 1}",
        f"Captured: {datetime.now().strftime('%d %b %Y, %H:%M:%S')}",
    ])
    return page_index + 1


def capture_ugc(row, out: Path, timeout=90):
    ugc = load_module("ugc_verify", DEMO / "ugc_verification" / "main.py")
    driver = ugc.build_driver(1700, 900, headless=True)
    try:
        driver.get(UGC_URL)
        ugc.wait_table_loaded(driver, timeout)
        ugc.click_category(driver, "all", timeout)
        ugc.show_all_entries(driver, timeout)
        found = driver.execute_script(ugc.FIND_JS, row["name"])
        if not found:
            raise LookupError(f"UGC list has no row for {row['name']}")
        listed_name = found[0][2]
        ugc.filter_table(driver, listed_name)
        driver.execute_script(ugc.HIGHLIGHT_JS, row["name"], False)
        out.parent.mkdir(parents=True, exist_ok=True)
        driver.save_screenshot(str(out))
        ugc.add_banner(out, [
            f"Source: {UGC_URL}",
            f"University: {listed_name}   |   {row['state'] or ''}",
            f"Captured: {datetime.now().strftime('%d %b %Y, %H:%M:%S')}",
        ])
    finally:
        driver.quit()


def _file_record(source, row, page=None):
    out = png_path(source, row["name"], row["state"])
    record = {
        "source": source,
        "file": f"{source}/{out.name}",
        "ready": out.exists(),
    }
    if out.exists():
        record["captured_at"] = datetime.fromtimestamp(out.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
    if page:
        record["page"] = page
    return record


def _summary(row, files):
    return {
        "name": row["name"],
        "state": row["state"],
        "city": row["city"],
        "primary_source": row["primary_source"],
        "sources": [item["source"] for item in files],
        "files": files,
    }


def describe(name, state=None, city=None):
    """JSON details for the screenshots this name maps to. Nothing is stored on disk."""
    row = fetch_institution(name, state, city)
    files = [_file_record(source, row) for source in applicable_sources(row)]
    if not any(item["ready"] for item in files):
        raise FileNotFoundError(f"screenshot has not been captured for {row['name']}")
    return _summary(row, files)


def capture(name, state=None, city=None, skip_existing=False, timeout=90):
    """Capture every list this university belongs to. Returns the JSON details."""
    row = fetch_institution(name, state, city)
    files = []
    for source in applicable_sources(row):
        out = png_path(source, row["name"], row["state"])
        page = None
        if not (skip_existing and out.exists()):
            if source == "aicte":
                page = capture_aicte(row, out)
            else:
                capture_ugc(row, out, timeout)
        files.append(_file_record(source, row, page))
    return _summary(row, files)


def main():
    parser = argparse.ArgumentParser(description="Capture university screenshots by name")
    parser.add_argument("--name", required=True, help="University name, for example Anjaneya University")
    parser.add_argument("--state", help="Required when more than one institution has this name")
    parser.add_argument("--city", help="Required when the name and state still match several institutions")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--timeout", type=int, default=90, help="Seconds to wait for the UGC table")
    args = parser.parse_args()
    try:
        meta = capture(args.name, args.state, args.city, args.skip_existing, args.timeout)
    except AmbiguousName as exc:
        print(exc)
        for match in exc.matches:
            print(f"  {match['name']}  |  {match['state'] or '-'}  |  {match['city'] or '-'}")
        raise SystemExit(2)
    print(f"name     {meta['name']}  |  {meta['state']}")
    print(f"sources  {', '.join(meta['sources'])}")
    for item in meta["files"]:
        print(f"saved    {item['file']}")


if __name__ == "__main__":
    main()
