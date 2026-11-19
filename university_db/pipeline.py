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

Batch mode captures many institutions in one run. Each institution is resolved
by its database id, so shared names are never a problem. Files are named by
name and state, and by city (then id) only when that is not unique.

    python pipeline.py --all                          every institution, every list it is on
    python pipeline.py --all --source ugc             every UGC university, UGC proof only
    python pipeline.py --all --source aicte           every AICTE institution, AICTE proof only
    python pipeline.py --all --source ugc --state Rajasthan --skip-existing
    python pipeline.py --all --dry-run                list what would be captured
"""
import argparse
import csv
import time
import importlib.util
import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pymupdf
import pymysql
from pymysql.cursors import DictCursor

ROOT = Path(__file__).resolve().parent
DEMO = ROOT.parent
SHOTS = ROOT / "screenshots"
LOG_DIR = ROOT / "logs"
log = logging.getLogger("pipeline")
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
    """Several institutions share this name. The caller must narrow it.

    `missing` says what to add: "state" or "city".
    """

    def __init__(self, message, matches, missing="state"):
        super().__init__(message)
        self.matches = matches
        self.missing = missing


def setup_logging(verbose=False):
    """Log to the console and to logs/pipeline.log. Safe to call more than once."""
    if getattr(setup_logging, "done", False):
        return
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    logfile = logging.handlers.RotatingFileHandler(
        LOG_DIR / "pipeline.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    logfile.setFormatter(fmt)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(console)
    log.addHandler(logfile)
    setup_logging.done = True


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


_dup_cache = {"at": 0.0, "state": {}, "city": {}}


def _dup_counts():
    """How many institutions share (name, state) and (name, state, city). Cached for a minute."""
    if time.time() - _dup_cache["at"] < 60 and _dup_cache["state"]:
        return _dup_cache["state"], _dup_cache["city"]
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name_key, state, city, COUNT(*) AS n FROM institutions GROUP BY name_key, state, city"
            )
            rows = cursor.fetchall()
    finally:
        connection.close()
    per_state, per_city = {}, {}
    for r in rows:
        ks = (r["name_key"], state_key(r["state"]))
        per_state[ks] = per_state.get(ks, 0) + r["n"]
        kc = ks + (name_key(r["city"]),)
        per_city[kc] = per_city.get(kc, 0) + r["n"]
    _dup_cache.update(at=time.time(), state=per_state, city=per_city)
    return per_state, per_city


def _piece(value, limit):
    return re.sub(r"[^A-Za-z0-9]+", "_", value or "").strip("_")[:limit]


def file_slug(row):
    """Name and state. City is added when name+state is shared, and the id when that is still not unique."""
    per_state, per_city = _dup_counts()
    nk = row.get("name_key") or name_key(row["name"])
    sk = state_key(row["state"])
    slug = _piece(row["name"], 80) or "university"
    if row["state"]:
        slug += "__" + _piece(row["state"], 40)
    if per_state.get((nk, sk), 0) > 1:
        if row["city"]:
            slug += "__" + _piece(row["city"], 40)
        if not row["city"] or per_city.get((nk, sk, name_key(row["city"])), 0) > 1:
            slug += f"__id{row['id']}"
    return slug


def check_source(source):
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")


def png_path(source, row):
    check_source(source)
    return SHOTS / source / f"{file_slug(row)}.png"


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


def _label(name, state=None, city=None):
    return " | ".join(part for part in (name, state, city) if part)


def fetch_institution(name, state=None, city=None):
    """Resolve one institution from its university name.

    Raises AmbiguousName when the name matches several institutions and the
    caller has not given enough (state, then city) to pick one.
    """
    key = name_key(name)
    if not key:
        raise ValueError("university name is required")
    log.info("lookup: name=%r state=%r city=%r", name, state, city)
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM institutions WHERE name_key = %s", (key,))
            rows = cursor.fetchall()
    finally:
        connection.close()
    log.info("lookup: %d institution(s) match the name %r", len(rows), name)

    if not rows:
        log.warning("lookup: no institution named %r", name)
        raise LookupError(f"no institution named {name}")

    if state:
        wanted = state_key(state)
        in_state = [row for row in rows if state_key(row["state"]) == wanted]
        if not in_state:
            available = sorted({row["state"] for row in rows if row["state"]})
            log.warning("lookup: %r has no match in state %r (available: %s)",
                        name, state, ", ".join(available))
            raise LookupError(
                f"no institution named {name} in {state}. "
                f"Available states: {', '.join(available) or 'none'}"
            )
        log.info("lookup: %d match(es) after state filter %r", len(in_state), state)
        rows = in_state
    if city:
        wanted_city = name_key(city)
        in_city = [row for row in rows if name_key(row["city"]) == wanted_city]
        if not in_city:
            available = sorted({row["city"] for row in rows if row["city"]})
            log.warning("lookup: %r has no match in city %r (available: %s)",
                        name, city, ", ".join(available))
            raise LookupError(
                f"no institution named {_label(name, state)} in city {city}. "
                f"Available cities: {', '.join(available) or 'none'}"
            )
        log.info("lookup: %d match(es) after city filter %r", len(in_city), city)
        rows = in_city

    states = {row["state"] or "" for row in rows}
    if state is None and len(states) > 1:
        choices = _choices(rows)
        log.warning("lookup: %r is ambiguous, %d institutions across %d states. State required.",
                    name, len(rows), len(states))
        raise AmbiguousName(
            f"{len(rows)} institutions are named {name} across {len(states)} states. "
            "Pass state to choose one.",
            choices,
            "state",
        )
    cities = {row["city"] or "" for row in rows if row["city"]}
    if city is None and len(cities) > 1:
        choices = _choices(rows)
        log.warning("lookup: %r is still ambiguous in %r, %d cities. City required.",
                    name, state, len(cities))
        raise AmbiguousName(
            f"{len(rows)} institutions named {_label(name, state)} are in different cities. "
            "Pass city to choose one.",
            choices,
            "city",
        )
    rows.sort(key=lambda row: (0 if row["in_ugc"] else 1, row["aicte_sr_no"] or 10**9, row["id"]))
    row = rows[0]
    if len(rows) > 1:
        log.warning("lookup: %d records are identical by name/state/city, using id %s",
                    len(rows), row["id"])
    log.info("lookup: resolved to %s (ugc=%s aicte=%s)",
             _label(row["name"], row["state"], row["city"]), bool(row["in_ugc"]), bool(row["in_aicte"]))
    return row


_verifier_cache = {}


def _verifier(kind):
    if kind not in _verifier_cache:
        folder, label = ("aicte_verification", "aicte_verify") if kind == "aicte" else ("ugc_verification", "ugc_verify")
        _verifier_cache[kind] = load_module(label, DEMO / folder / "main.py")
    return _verifier_cache[kind]


class AicteSession:
    """Opens the AICTE PDF once and indexes every permanent id by page."""

    def __init__(self):
        self.doc = None
        self.index = {}

    def _open(self):
        if not PDF_PATH.exists():
            raise FileNotFoundError(f"AICTE PDF not found: {PDF_PATH}")
        started = time.time()
        self.doc = pymupdf.open(PDF_PATH)
        for number, page in enumerate(self.doc):
            for word in page.get_text("words"):
                if re.fullmatch(r"\d-\d+", word[4]):
                    self.index.setdefault(word[4], number)
        log.info("aicte: indexed %d permanent ids across %d pages in %.1fs",
                 len(self.index), len(self.doc), time.time() - started)

    def capture(self, row, out: Path):
        permanent_id = row["aicte_permanent_id"]
        if not permanent_id:
            raise LookupError(f"{row['name']} has no AICTE permanent id")
        if self.doc is None:
            self._open()
        page_index = self.index.get(permanent_id)
        if page_index is None:
            raise LookupError(f"{row['name']} ({permanent_id}) was not found in the AICTE PDF")
        page = self.doc[page_index]
        rects = []
        for rect in page.search_for(permanent_id):
            words = page.get_text("words", clip=rect + pymupdf.Rect(-1, -2, 20, 2))
            if any(word[4] == permanent_id for word in words):
                rects.append(rect)
        if not rects:
            raise LookupError(f"{row['name']} ({permanent_id}) could not be located on page {page_index + 1}")
        verifier = _verifier("aicte")
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            verifier.capture(self.doc, page_index, rects, out, full_page=False)
        finally:
            for annot in list(page.annots() or []):  # keep the shared document clean for the next row
                page.delete_annot(annot)
        verifier.add_banner(out, [
            f"Source: {AICTE_PDF_URL}",
            f"University: {row['name']}   |   {row['state'] or ''}   |   Page {page_index + 1}",
            f"Captured: {datetime.now().strftime('%d %b %Y, %H:%M:%S')}",
        ])
        return page_index + 1

    def close(self):
        if self.doc is not None:
            self.doc.close()
            self.doc = None


# Keep only this university: exact name, this state, and (when several share both)
# the row whose address is the one stored in the database. Everything else is hidden.
FOCUS_JS = r"""
const norm = t => (t || '').toLowerCase().replace(/[^a-z0-9]/g, '');
const name = norm(arguments[0]), st = arguments[1], addr = arguments[2];
if (!name) return 0;
const rows = [];
document.querySelectorAll('#tbl tbody tr').forEach(tr => {
  const tds = tr.querySelectorAll('td');
  if (tds.length < 7) return;
  const ok = norm(tds[2].textContent) === name && (!st || norm(tds[5].textContent) === st);
  rows.push({tr, ok, addrOk: ok && !!addr && norm(tds[3].textContent) === addr});
});
const exact = rows.some(r => r.addrOk);
let n = 0;
rows.forEach(r => {
  if (exact ? r.addrOk : r.ok) { n++; }
  else { r.tr.dataset.hid = '1'; r.tr.style.display = 'none'; }
});
return n;
"""

# Undo FOCUS_JS and the highlight so the next row starts from a clean table.
CLEAR_JS = r"""
document.querySelectorAll('#tbl tbody tr').forEach(tr => {
  if (tr.dataset.hid) { tr.style.display = ''; delete tr.dataset.hid; }
  tr.style.boxShadow = '';
});
document.querySelectorAll('#tbl mark').forEach(m => {
  const p = m.parentNode;
  while (m.firstChild) p.insertBefore(m.firstChild, m);
  p.removeChild(m);
  p.normalize();
});
"""


class UgcSession:
    """One headless Chrome with the UGC table loaded, reused for every university."""

    def __init__(self, timeout=90):
        self.timeout = timeout
        self.driver = None

    def _start(self):
        ugc = _verifier("ugc")
        log.info("ugc: starting Chrome and loading the UGC list")
        self.driver = ugc.build_driver(1700, 900, headless=True)
        try:
            self.driver.get(UGC_URL)
            ugc.wait_table_loaded(self.driver, self.timeout)
            ugc.click_category(self.driver, "all", self.timeout)
            ugc.show_all_entries(self.driver, self.timeout)
        except Exception:
            self.close()
            raise
        log.info("ugc: table ready")

    def capture(self, row, out: Path):
        ugc = _verifier("ugc")
        if self.driver is None:
            self._start()
        driver = self.driver
        state = row["state"]
        state_alnum = re.sub(r"[^a-z0-9]", "", (state or "").lower())
        try:
            driver.execute_script(CLEAR_JS)
            ugc.filter_table(driver, f'"{row["name"]}"' + (f" {state}" if state else ""))
            if not driver.execute_script(FOCUS_JS, row["name"], state_alnum, re.sub(r"[^a-z0-9]", "", (row.get("address") or "").lower())):
                raise LookupError(f"UGC list has no row for {_label(row['name'], state)}")
            driver.execute_script(ugc.HIGHLIGHT_JS, row["name"], False)
            out.parent.mkdir(parents=True, exist_ok=True)
            driver.save_screenshot(str(out))
            ugc.add_banner(out, [
                f"Source: {UGC_URL}",
                f"University: {row['name']}   |   {state or ''}",
                f"Captured: {datetime.now().strftime('%d %b %Y, %H:%M:%S')}",
            ])
        except LookupError:
            raise
        except Exception:
            log.warning("ugc: browser error, restarting Chrome before the next row")
            self.close()
            raise
        finally:
            if self.driver is not None:
                try:
                    self.driver.execute_script(CLEAR_JS)
                except Exception:
                    pass

    def close(self):
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None


class Sessions:
    """Lazy per-source capture sessions. Chrome only starts if a UGC proof is needed."""

    def __init__(self, timeout=90):
        self.ugc = UgcSession(timeout)
        self.aicte = AicteSession()

    def capture(self, source, row, out):
        if source == "aicte":
            return self.aicte.capture(row, out)
        self.ugc.capture(row, out)
        return None

    def close(self):
        self.ugc.close()
        self.aicte.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _file_record(source, row, page=None):
    out = png_path(source, row)
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


def capture_row(row, sessions, sources=None, skip_existing=False):
    """Capture the given lists (default: every list the row is on). Returns the file records."""
    files = []
    for source in sources or applicable_sources(row):
        out = png_path(source, row)
        page = None
        if skip_existing and out.exists():
            log.info("capture[%s]: skipped, %s already exists", source, out.name)
        else:
            log.info("capture[%s]: started", source)
            try:
                page = sessions.capture(source, row, out)
            except Exception:
                log.exception("capture[%s]: failed for %s", source, _label(row["name"], row["state"]))
                raise
            log.info("capture[%s]: saved %s", source, out)
        files.append(_file_record(source, row, page))
    return files


def capture(name, state=None, city=None, skip_existing=False, timeout=90):
    """Capture every list this university belongs to. Returns the JSON details."""
    row = fetch_institution(name, state, city)
    sources = applicable_sources(row)
    log.info("capture: %s -> lists %s", _label(row["name"], row["state"]), ", ".join(sources))
    with Sessions(timeout) as sessions:
        files = capture_row(row, sessions, sources, skip_existing)
    return _summary(row, files)


BATCH_SOURCES = ("all",) + SOURCES


def select_institutions(source="all", state=None, limit=None):
    """Institutions for a batch run, in a stable order. Each row is one institution."""
    if source not in BATCH_SOURCES:
        raise ValueError(f"source must be one of {BATCH_SOURCES}")
    where, params = [], []
    if source == "ugc":
        where.append("in_ugc = 1")
    elif source == "aicte":
        where.append("in_aicte = 1")
    if state:
        where.append("state IS NOT NULL")
    sql = "SELECT * FROM institutions" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY name, state, city, id"
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
    finally:
        connection.close()
    if state:
        wanted = state_key(state)
        rows = [row for row in rows if state_key(row["state"]) == wanted]
    if limit:
        rows = rows[:limit]
    return rows


def capture_all(source="all", state=None, limit=None, skip_existing=False, timeout=90,
                dry_run=False, progress=None, should_stop=None):
    """Capture proofs for every institution on the chosen list(s).

    One failing institution never stops the run. It is logged, written to the
    CSV report under logs/, and counted. `progress` (a dict) is updated live so
    a caller such as the API can poll it. Returns the summary dict.
    """
    rows = select_institutions(source, state, limit)
    tasks = []
    for row in rows:
        for list_name in applicable_sources(row) if (row["in_ugc"] or row["in_aicte"]) else []:
            if source in ("all", list_name):
                tasks.append((row, list_name))
    progress = progress if progress is not None else {}
    progress.update(source=source, state=state, total=len(tasks), done=0, saved=0, skipped=0,
                    failed=0, dry_run=dry_run, finished=False, current=None, report=None)
    log.info("batch: source=%s state=%s limit=%s skip_existing=%s dry_run=%s -> %d institutions, %d proofs",
             source, state, limit, skip_existing, dry_run, len(rows), len(tasks))

    LOG_DIR.mkdir(exist_ok=True)
    report_path = LOG_DIR / f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    progress["report"] = str(report_path)
    started = time.time()
    failures = []
    with open(report_path, "w", newline="", encoding="utf-8") as handle, Sessions(timeout) as sessions:
        writer = csv.writer(handle)
        writer.writerow(["id", "name", "state", "city", "source", "status", "file", "error"])
        try:
            for number, (row, list_name) in enumerate(tasks, 1):
                if should_stop and should_stop():
                    log.warning("batch: stop requested after %d of %d", number - 1, len(tasks))
                    break
                label = _label(row["name"], row["state"], row["city"])
                progress["current"] = f"{list_name}: {label}"
                out = png_path(list_name, row)
                rel = f"{list_name}/{out.name}"
                error = ""
                if dry_run:
                    status = "planned"
                elif skip_existing and out.exists():
                    status = "skipped"
                    progress["skipped"] += 1
                else:
                    try:
                        sessions.capture(list_name, row, out)
                        status = "saved"
                        progress["saved"] += 1
                    except Exception as exc:
                        status, error = "failed", f"{type(exc).__name__}: {exc}"
                        progress["failed"] += 1
                        failures.append({"name": row["name"], "state": row["state"],
                                         "source": list_name, "error": error})
                        log.error("batch: [%d/%d] %s %s FAILED: %s", number, len(tasks), list_name, label, error)
                if status != "failed":
                    log.info("batch: [%d/%d] %s %s -> %s", number, len(tasks), list_name, label, status)
                writer.writerow([row["id"], row["name"], row["state"] or "", row["city"] or "",
                                 list_name, status, rel, error])
                handle.flush()
                progress["done"] = number
                if number % 25 == 0:
                    rate = (time.time() - started) / number
                    log.info("batch: progress %d/%d, %.1fs per proof, about %.0f min left",
                             number, len(tasks), rate, rate * (len(tasks) - number) / 60)
        except KeyboardInterrupt:
            log.warning("batch: interrupted by user at %d of %d", progress["done"], len(tasks))
    progress.update(finished=True, current=None, seconds=round(time.time() - started, 1))
    log.info("batch: finished. saved=%d skipped=%d failed=%d of %d in %.0fs. report: %s",
             progress["saved"], progress["skipped"], progress["failed"], len(tasks),
             time.time() - started, report_path)
    return {**progress, "failures": failures}


def choose_interactively(exc):
    """Ask the user to pick from an ambiguous list. Returns the chosen match."""
    print(exc, file=sys.stderr)
    for number, match in enumerate(exc.matches, 1):
        print(f"  {number:>2}. {match['name']}  |  {match['state'] or '-'}  |  {match['city'] or '-'}",
              file=sys.stderr)
    while True:
        answer = input(f"Enter a number (1-{len(exc.matches)}) or the {exc.missing} (blank to cancel): ").strip()
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(exc.matches):
            return exc.matches[int(answer) - 1]
        wanted = state_key(answer) if exc.missing == "state" else name_key(answer)
        field = "state" if exc.missing == "state" else "city"
        hits = [m for m in exc.matches if (state_key(m[field]) if field == "state" else name_key(m[field])) == wanted]
        if hits:
            return hits[0]
        print("Not one of the listed options.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Capture university screenshots by name")
    parser.add_argument("--name", help="University name, for example Anjaneya University")
    parser.add_argument("--all", action="store_true", help="Batch mode: capture every institution on the chosen list(s)")
    parser.add_argument("--source", choices=BATCH_SOURCES, default="all",
                        help="With --all: ugc, aicte, or all (every list each institution is on)")
    parser.add_argument("--limit", type=int, help="With --all: stop after this many institutions")
    parser.add_argument("--dry-run", action="store_true", help="With --all: list what would be captured and write nothing")
    parser.add_argument("--state", help="Required when more than one institution has this name")
    parser.add_argument("--city", help="Required when the name and state still match several institutions")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--timeout", type=int, default=90, help="Seconds to wait for the UGC table")
    parser.add_argument("--no-prompt", action="store_true",
                        help="Never ask for the state or city. Print the choices and exit with code 2")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.all:
        if args.name or args.city:
            parser.error("--all cannot be combined with --name or --city (--state is allowed as a filter)")
        try:
            result = capture_all(args.source, args.state, args.limit, args.skip_existing,
                                 args.timeout, args.dry_run)
        except Exception as exc:
            log.exception("batch failed: %s", exc)
            raise SystemExit(1)
        print(f"total    {result['total']}")
        print(f"saved    {result['saved']}")
        print(f"skipped  {result['skipped']}")
        print(f"failed   {result['failed']}")
        print(f"report   {result['report']}")
        raise SystemExit(1 if result["failed"] else 0)
    if not args.name:
        parser.error("pass --name, or --all for every institution")

    state, city = args.state, args.city
    interactive = sys.stdin.isatty() and not args.no_prompt
    while True:
        try:
            meta = capture(args.name, state, city, args.skip_existing, args.timeout)
            break
        except AmbiguousName as exc:
            if not interactive:
                print(exc, file=sys.stderr)
                for match in exc.matches:
                    print(f"  {match['name']}  |  {match['state'] or '-'}  |  {match['city'] or '-'}",
                          file=sys.stderr)
                print(f'Re-run with --{exc.missing} "<value>".', file=sys.stderr)
                raise SystemExit(2)
            picked = choose_interactively(exc)
            if picked is None:
                log.info("cancelled by user at the %s prompt", exc.missing)
                raise SystemExit(2)
            log.info("user chose %s", _label(picked["name"], picked["state"], picked["city"]))
            if exc.missing == "state":
                state = picked["state"]
            else:
                city = picked["city"]
        except LookupError as exc:
            log.error("%s", exc)
            raise SystemExit(1)
    print(f"name     {meta['name']}  |  {meta['state']}")
    print(f"sources  {', '.join(meta['sources'])}")
    for item in meta["files"]:
        print(f"saved    {item['file']}")


if __name__ == "__main__":
    main()
