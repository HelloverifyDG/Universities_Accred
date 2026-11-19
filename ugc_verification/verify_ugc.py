#!/usr/bin/env python3
"""
Verify a university against the UGC list by calling the same endpoint the
website uses (Getuniversity_details) - no browser, no screenshot.

Install:  pip install requests
Usage:    python verify_ugc_api.py "CMJ University"
          python verify_ugc_api.py "APJ ABDUL KALAM TECHNOLOGICAL UNIVERSITY" --save result.json
          python verify_ugc_api.py "CMJ University" --dump     # save raw responses to inspect the format
"""
import argparse
import difflib
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

BASE = "https://www.ugc.gov.in"
ENDPOINT = BASE + "/universitydetails/Getuniversity_details"
UNITYPE_IDS = [1, 0]  # the two calls the website makes
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "referer": BASE + "/",
    "x-requested-with": "XMLHttpRequest",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0"),
}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(s).lower())).strip()


def parse_response(resp: requests.Response):
    """Return a list of records (dicts). Handles plain JSON, double-encoded JSON,
    ASP.NET {"d": ...} wrappers, and HTML tables."""
    text = resp.text.strip()
    try:
        data = json.loads(text)
        for _ in range(3):  # unwrap {"d": ...} and JSON-in-a-string
            if isinstance(data, dict) and set(data) == {"d"}:
                data = data["d"]
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    break
        return list(iter_records(data))
    except ValueError:
        pass

    # HTML fallback: one record per <tr>
    records = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.S | re.I):
        cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)) for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S | re.I)]
        cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
        if any(cells):
            records.append({f"col{i}": c for i, c in enumerate(cells)})
    return records


def iter_records(obj):
    """Yield every dict that looks like a row, wherever it is nested."""
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                yield item
            else:
                yield from iter_records(item)
    elif isinstance(obj, dict):
        lists = [v for v in obj.values() if isinstance(v, (list, dict))]
        if lists:
            for v in lists:
                yield from iter_records(v)
        else:
            yield obj


def record_name(rec: dict) -> str:
    for k, v in rec.items():
        if "name" in k.lower() and isinstance(v, str) and v.strip():
            return v.strip()
    strings = [str(v) for v in rec.values() if isinstance(v, str) and v.strip()]
    return max(strings, key=len) if strings else ""


def fetch_all(session, verify_ssl, dump_dir=None):
    all_records = []
    for uid in UNITYPE_IDS:
        r = session.get(ENDPOINT, params={"unitypeID": uid}, headers=HEADERS,
                        timeout=60, verify=verify_ssl)
        r.raise_for_status()
        if dump_dir:
            ext = "json" if "json" in r.headers.get("content-type", "") else "txt"
            (dump_dir / f"unitypeID_{uid}.{ext}").write_text(r.text, encoding="utf-8")
        recs = parse_response(r)
        print(f"  unitypeID={uid}: HTTP {r.status_code}, {len(recs)} record(s)")
        for rec in recs:
            rec["_unitypeID"] = uid
        all_records.extend(recs)
    return all_records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="University name to verify")
    ap.add_argument("--save", help="Write the result (with timestamp) to this JSON file")
    ap.add_argument("--dump", action="store_true", help="Save raw API responses to ./api_dump/")
    ap.add_argument("--no-verify-ssl", action="store_true")
    args = ap.parse_args()

    dump_dir = None
    if args.dump:
        dump_dir = Path("api_dump")
        dump_dir.mkdir(exist_ok=True)

    session = requests.Session()
    verify_ssl = not args.no_verify_ssl
    print("Fetching UGC data...")
    session.get(BASE + "/", headers=HEADERS, timeout=60, verify=verify_ssl)  # picks up the session cookie
    records = fetch_all(session, verify_ssl, dump_dir)

    if not records:
        print("ERROR: no records parsed. Re-run with --dump and check api_dump/ for the raw response.")
        sys.exit(2)

    q = norm(args.name)
    matches, seen = [], set()
    for rec in records:
        flat = norm(" ".join(str(v) for k, v in rec.items() if k != "_unitypeID"))
        if q in flat:
            key = json.dumps(rec, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                matches.append(rec)

    result = {
        "searched": args.name,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "source": ENDPOINT,
        "found": bool(matches),
        "matches": matches,
    }

    if matches:
        print(f"\nFOUND: {len(matches)} match(es) for '{args.name}'")
        for m in matches:
            print(json.dumps({k: v for k, v in m.items() if k != "_unitypeID"},
                             indent=2, ensure_ascii=False))
    else:
        print(f"\nNOT FOUND: '{args.name}' is not in the UGC list.")
        names = sorted({record_name(r) for r in records if record_name(r)})
        close = difflib.get_close_matches(args.name, names, n=5, cutoff=0.5)
        if close:
            print("Closest names in the list:")
            for c in close:
                print(f"  - {c}")
        result["closest"] = close

    if args.save:
        Path(args.save).write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str),
                                   encoding="utf-8")
        print(f"\nSaved result -> {args.save}")

    sys.exit(0 if matches else 1)


if __name__ == "__main__":
    main()