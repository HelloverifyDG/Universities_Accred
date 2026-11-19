#!/usr/bin/env python3
"""
Load UGC universities and AICTE approved institutions into one MySQL table.

UGC source:  the public list behind "View All" on
             https://www.ugc.gov.in/universitydetails/university
AICTE source: the Groupwise Bifurcation PDF for Approval Process 2025-26.

Overlap rule: a UGC university and an AICTE institution are the same record
when their names match after punctuation is removed and their states match.
The stored row keeps the UGC name, address, type, status, and website.
AICTE permanent id, region, district, city, and user group are added to that
same row. Institutions that appear on only one list stay as their own rows.

Only the columns published on those two lists are stored.

Usage:
    python load_institutions.py
    python load_institutions.py --pdf ../aicte_verification/aicte_list.pdf

Connection (defaults work for a local root login with no password):
    MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE
"""
import argparse
import os
import re
from collections import defaultdict
from pathlib import Path

import pymupdf
import pymysql
import requests

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "schema.sql"
DEFAULT_PDF = HERE.parent / "aicte_verification" / "aicte_list.pdf"
UGC_URL = "https://www.ugc.gov.in/universitydetails/Getuniversity_details"
UGC_PAGE = "https://www.ugc.gov.in/universitydetails/university?type=ddmCMsxJZgXH2S/m0uMOKQ=="

# Column x-ranges measured from the PDF table grid (points).
COLUMN_BOUNDS = (
    ("sr", 0.0, 45.72),
    ("pid", 45.72, 110.54),
    ("name", 110.54, 265.73),
    ("region", 265.73, 317.35),
    ("state", 317.35, 381.43),
    ("district", 381.43, 449.38),
    ("city", 449.38, 521.62),
    ("group", 521.62, 640.0),
)

KNOWN_STATES = {
    "andaman and nicobar islands",
    "andhra pradesh",
    "arunachal pradesh",
    "assam",
    "bihar",
    "chandigarh",
    "chhattisgarh",
    "dadra and nagar haveli",
    "dadra and nagar haveli and daman and diu",
    "daman and diu",
    "delhi",
    "goa",
    "gujarat",
    "haryana",
    "himachal pradesh",
    "jammu and kashmir",
    "jharkhand",
    "karnataka",
    "kerala",
    "ladakh",
    "lakshadweep",
    "madhya pradesh",
    "maharashtra",
    "manipur",
    "meghalaya",
    "mizoram",
    "nagaland",
    "odisha",
    "puducherry",
    "punjab",
    "rajasthan",
    "sikkim",
    "tamil nadu",
    "telangana",
    "tripura",
    "uttar pradesh",
    "uttarakhand",
    "west bengal",
}

STATE_ALIASES = {
    "orissa": "odisha",
    "pondicherry": "puducherry",
    "uttaranchal": "uttarakhand",
    "nctofdelhi": "delhi",
}


def clean(value):
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip(" \t\r\n,;")
    return text or None


def name_key(value):
    text = (value or "").upper().replace("&", " AND ")
    return re.sub(r"[^A-Z0-9]", "", text)


def state_key(value):
    key = re.sub(r"[^a-z]", "", (value or "").lower())
    return STATE_ALIASES.get(key, key)


def column_for(x_center):
    for name, left, right in COLUMN_BOUNDS:
        if left <= x_center < right:
            return name
    return None


# A short token alone on the next line is a word the PDF broke in half
# ("THIRUVANANTHAP" / "URAM"). Real words such as OF and AND stay separate.
STANDALONE_WORDS = {"OF", "AND", "THE", "FOR", "IN", "AT", "BY", "A", "AN", "TO", "DE", "ON"}


def join_words(items):
    if not items:
        return None
    items.sort()
    lines = []
    for y, _x, text in items:
        if lines and abs(y - lines[-1][0]) <= 2:
            lines[-1][1].append(text)
        else:
            lines.append([y, [text]])
    parts = []
    for _y, tokens in lines:
        token = tokens[0]
        previous = parts[-1].split()[-1] if parts else ""
        if (len(tokens) == 1 and len(token) <= 4 and len(previous) >= 8
                and token.upper() not in STANDALONE_WORDS):
            parts[-1] += token
        else:
            parts.append(" ".join(tokens))
    return clean(" ".join(parts))


def parse_aicte_pdf(path: Path):
    """Read every institution row from the groupwise PDF."""
    doc = pymupdf.open(path)
    rows = []
    for page_number, page in enumerate(doc, start=1):
        words = page.get_text("words")
        anchors = sorted(
            y0 for x0, y0, _x1, _y1, text, *_rest in words
            if re.fullmatch(r"\d+-\d+", text) and 45 <= x0 < 110
        )
        if not anchors:
            continue
        buckets = [{name: [] for name, _, _ in COLUMN_BOUNDS} for _ in anchors]
        for x0, y0, x1, y1, text, *_rest in words:
            column = column_for((x0 + x1) / 2)
            if column is None or y0 < 48:
                continue
            for index, anchor in enumerate(anchors):
                top = (anchors[index - 1] + anchor) / 2 if index else anchor - 14
                bottom = (anchor + anchors[index + 1]) / 2 if index + 1 < len(anchors) else anchor + 18
                if top <= y0 < bottom:
                    buckets[index][column].append((y0, x0, text))
                    break
        for bucket in buckets:
            record = {name: join_words(items) for name, items in bucket.items()}
            record["page"] = page_number
            if record["pid"] and record["name"] and re.fullmatch(r"\d+-\d+", record["pid"]):
                record["state"], record["district"] = repair_state(record["state"], record["district"])
                rows.append(record)
    return rows


def repair_state(state, district):
    """Pull a district token back into a state name that overflowed its column."""
    state = state or ""
    district = district or ""
    if state.lower() in KNOWN_STATES:
        return clean(state), clean(district)
    state_tokens = state.split()
    district_tokens = district.split()
    for take in range(1, len(district_tokens) + 1):
        candidate = " ".join(state_tokens + district_tokens[:take])
        if candidate.lower() in KNOWN_STATES:
            return clean(candidate), clean(" ".join(district_tokens[take:]))
    return clean(state), clean(district)


def fetch_ugc(timeout=60):
    """Download the full UGC list (unitypeID 0 is the 'View All' set)."""
    response = requests.get(
        UGC_URL,
        params={"unitypeID": "0"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    universities = payload.get("List") or []
    if len(universities) < 1000:
        raise RuntimeError(f"UGC returned only {len(universities)} universities")
    return universities


def ugc_record(item):
    return {
        "ugc_id": clean(item.get("ID")),
        "name": clean(item.get("uni_name")),
        "ugc_type": clean(item.get("uni_type")),
        "address": clean(item.get("address")),
        "zip": clean(item.get("Zip")),
        "state": clean(item.get("state")),
        "ugc_status": clean(item.get("status")),
        "website_url": clean(item.get("url")),
    }


def merge(ugc_rows, aicte_rows):
    """Prefer UGC. Attach every AICTE hit with the same name and state."""
    ugc_groups = defaultdict(list)
    for row in ugc_rows:
        ugc_groups[(name_key(row["name"]), state_key(row["state"]))].append(row)

    aicte_groups = defaultdict(list)
    for row in aicte_rows:
        aicte_groups[(name_key(row["name"]), state_key(row["state"]))].append(row)

    consumed = set()
    merged = []
    ambiguous = 0
    for row in ugc_rows:
        key = (name_key(row["name"]), state_key(row["state"]))
        matches = []
        if len(ugc_groups[key]) == 1:
            matches = sorted(aicte_groups.get(key, []), key=lambda item: int(item["sr"]))
        elif aicte_groups.get(key):
            ambiguous += 1
        for match in matches:
            consumed.add(match["pid"])
        primary = matches[0] if matches else None
        merged.append({
            "name": row["name"],
            "name_key": key[0],
            "state": row["state"],
            "primary_source": "ugc",
            "in_ugc": 1,
            "in_aicte": 1 if matches else 0,
            "ugc_id": row["ugc_id"],
            "ugc_type": row["ugc_type"],
            "address": row["address"],
            "zip": row["zip"],
            "ugc_status": row["ugc_status"],
            "website_url": row["website_url"],
            "aicte_permanent_id": primary["pid"] if primary else None,
            "aicte_permanent_ids": ",".join(item["pid"] for item in matches) or None,
            "aicte_sr_no": int(primary["sr"]) if primary else None,
            "region": primary["region"] if primary else None,
            "district": primary["district"] if primary else None,
            "city": primary["city"] if primary else None,
            "user_group": primary["group"] if primary else None,
            "aicte_match_count": len(matches),
            "record_key": f"ugc:{row['ugc_id']}",
        })

    for row in aicte_rows:
        if row["pid"] in consumed:
            continue
        merged.append({
            "name": row["name"],
            "name_key": name_key(row["name"]),
            "state": row["state"],
            "primary_source": "aicte",
            "in_ugc": 0,
            "in_aicte": 1,
            "ugc_id": None,
            "ugc_type": None,
            "address": None,
            "zip": None,
            "ugc_status": None,
            "website_url": None,
            "aicte_permanent_id": row["pid"],
            "aicte_permanent_ids": row["pid"],
            "aicte_sr_no": int(row["sr"]) if row["sr"] and row["sr"].isdigit() else None,
            "region": row["region"],
            "district": row["district"],
            "city": row["city"],
            "user_group": row["group"],
            "aicte_match_count": 1,
            "record_key": f"aicte:{row['pid']}",
        })
    return merged, ambiguous


def apply_schema(connection):
    script = SCHEMA_PATH.read_text(encoding="utf-8")
    statements = [part.strip() for part in script.split(";") if part.strip()]
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    connection.commit()


def load(connection, rows):
    columns = (
        "name", "name_key", "state", "primary_source", "in_ugc", "in_aicte",
        "ugc_id", "ugc_type", "address", "zip", "ugc_status", "website_url",
        "aicte_permanent_id", "aicte_permanent_ids", "aicte_sr_no",
        "region", "district", "city", "user_group", "aicte_match_count", "record_key",
    )
    sql = (
        "INSERT INTO institutions (" + ", ".join(columns) + ") VALUES ("
        + ", ".join(["%s"] * len(columns)) + ")"
    )
    values = [tuple(row[column] for column in columns) for row in rows]
    with connection.cursor() as cursor:
        cursor.execute("USE university_registry")
        for start in range(0, len(values), 1000):
            cursor.executemany(sql, values[start:start + 1000])
    connection.commit()


def connect():
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "root"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        charset="utf8mb4",
        autocommit=False,
    )


def summarize(aicte_rows, ugc_rows, merged, ambiguous):
    both = sum(1 for row in merged if row["in_ugc"] and row["in_aicte"])
    ugc_only = sum(1 for row in merged if row["in_ugc"] and not row["in_aicte"])
    aicte_only = sum(1 for row in merged if row["in_aicte"] and not row["in_ugc"])
    unknown_states = sorted({
        row["state"] for row in aicte_rows
        if row["state"] and row["state"].lower() not in KNOWN_STATES
    })
    print(f"UGC universities fetched: {len(ugc_rows)}")
    print(f"AICTE institutions parsed: {len(aicte_rows)}")
    print(f"Rows in institutions:      {len(merged)}")
    print(f"  on both, UGC kept:       {both}")
    print(f"  UGC only:                {ugc_only}")
    print(f"  AICTE only:              {aicte_only}")
    print(f"Ambiguous UGC name+state (not merged): {ambiguous}")
    if unknown_states:
        print("AICTE states outside the known list:")
        for state in unknown_states:
            print(f"  {state}")
    print(f"Source page: {UGC_PAGE}")


def main():
    parser = argparse.ArgumentParser(description="Load UGC and AICTE into one MySQL table")
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    args = parser.parse_args()
    if not args.pdf.exists():
        raise SystemExit(f"AICTE PDF not found: {args.pdf}")

    print("Fetching UGC university list...")
    ugc_rows = [ugc_record(item) for item in fetch_ugc() if clean(item.get("uni_name"))]
    print(f"Parsing {args.pdf} ...")
    aicte_rows = parse_aicte_pdf(args.pdf)
    if len(aicte_rows) < 10000:
        raise SystemExit(f"AICTE parse looks incomplete: {len(aicte_rows)} rows")

    merged, ambiguous = merge(ugc_rows, aicte_rows)
    summarize(aicte_rows, ugc_rows, merged, ambiguous)

    print("Writing MySQL database university_registry.institutions ...")
    connection = connect()
    try:
        apply_schema(connection)
        load(connection, merged)
    finally:
        connection.close()
    print("Done.")


if __name__ == "__main__":
    main()
