#!/usr/bin/env python3
"""
Open the UGC university list in headless Chrome, click "View All", set
"Show entries" to All, find the university, highlight it and save a
screenshot with a proof banner (URL + timestamp).

Install:  pip install selenium pillow
Usage:    python verify_ugc.py "A.P.J. Abdul Kalam Technological University"
          python verify_ugc.py "APJ Abdul Kalam Technological University"   # dots/spaces are ignored
          python verify_ugc.py "Adani University" -o proofs --category private
          python verify_ugc.py "Adani University" --no-filter   # keep the full list, scroll to the row
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from selenium import webdriver
from selenium.common.exceptions import (NoSuchElementException,
                                        StaleElementReferenceException,
                                        TimeoutException)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait

PAGE_URL = "https://www.ugc.gov.in/universitydetails/university?type=ddmCMsxJZgXH2S/m0uMOKQ=="
DEFAULT_NAME = "A.P.J. Abdul Kalam Technological University"

CATEGORY_BUTTONS = {
    "all": "btnall",
    "central": "btncentral",
    "state": "btnstate",
    "deemed": "btndeemed",
    "private": "btnstprivate",
}

# Finds rows whose "Name of the University" cell (3rd column) matches the query.
# Punctuation, dots and spacing are ignored: "APJ" matches "A.P.J.".
FIND_JS = r"""
const alnum = arguments[0].toLowerCase().replace(/[^a-z0-9]/g, '');
if (!alnum) return [];
const re = new RegExp(alnum.split('').join('[^a-z0-9]*'), 'i');
const out = [];
document.querySelectorAll('#tbl tbody tr').forEach(tr => {
  const tds = tr.querySelectorAll('td');
  if (tds.length < 7) return;
  if (re.test(tds[2].textContent)) {
    out.push([0, 1, 2, 5, 6].map(i => tds[i].textContent.replace(/\s+/g, ' ').trim()));
  }
});
return out;
"""

# Highlights the matching text inside the name column and scrolls to the table.
HIGHLIGHT_JS = r"""
const alnum = arguments[0].toLowerCase().replace(/[^a-z0-9]/g, '');
const scrollToRow = arguments[1];
const re = new RegExp(alnum.split('').join('[^a-z0-9]*'), 'i');
let count = 0, first = null;
document.querySelectorAll('#tbl tbody tr').forEach(tr => {
  const td = tr.querySelectorAll('td')[2];
  if (!td) return;
  const walker = document.createTreeWalker(td, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const m = re.exec(node.nodeValue);
    if (!m) continue;
    const mid = node.splitText(m.index);
    mid.splitText(m[0].length);
    const mark = document.createElement('mark');
    mark.style.cssText = 'background:#9acd00;color:#000;padding:2px 0;outline:2px solid #6b8e00;';
    mid.parentNode.replaceChild(mark, mid);
    mark.appendChild(mid);
    tr.style.boxShadow = 'inset 0 0 0 2px #6b8e00';
    count++;
    if (!first) first = mark;
  }
});
if (first) {
  if (scrollToRow) first.scrollIntoView({block: 'center'});
  else (document.querySelector('#tbl_wrapper .dtsp-dataTable') || document.querySelector('#tbl_wrapper'))
         .scrollIntoView({block: 'start'});
}
return count;
"""


def build_driver(width, height, headless=True):
    os.environ.setdefault("SE_AVOID_STATS", "true")  # stops the harmless Plausible stats error
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument(f"--window-size={width},{height}")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")  # needed in WSL/Docker
    opts.add_argument("--disable-gpu")
    opts.add_argument("--hide-scrollbars")
    opts.add_argument("--lang=en-US")
    return webdriver.Chrome(options=opts)


def info_text(driver):
    els = driver.find_elements(By.ID, "tbl_info")
    return els[0].text if els else ""


def wait_table_loaded(driver, timeout):
    """Wait until the table has data rows (the info line is not required)."""
    WebDriverWait(driver, timeout).until(
        lambda d: bool(d.find_elements(By.CSS_SELECTOR, "#tbl tbody tr td")))


def click_category(driver, category, timeout):
    btn = driver.find_element(By.ID, CATEGORY_BUTTONS[category])
    # "View All" is already the active default (it carries the 'bgcolor' class).
    # Clicking it again makes the site rebuild the table, which is what hung before.
    if "bgcolor" in (btn.get_attribute("class") or ""):
        print(f"'{category}' is already selected - no click needed")
        return
    driver.execute_script("arguments[0].click();", btn)  # JS click avoids overlay issues
    time.sleep(2)  # the table is rebuilt after the click
    wait_table_loaded(driver, timeout)


def show_all_entries(driver, timeout):
    def pick_all(d):
        try:  # retry: the select can go stale while the table is rebuilt
            Select(d.find_element(By.CSS_SELECTOR, "select[name='tbl_length']")).select_by_value("-1")
            return True
        except (StaleElementReferenceException, NoSuchElementException):
            return False
    WebDriverWait(driver, timeout).until(pick_all)

    def all_shown(d):
        m = re.search(r"Showing 1 to ([\d,]+) of ([\d,]+) entries", info_text(d))
        if m and m.group(1) == m.group(2):
            return True
        return len(d.find_elements(By.CSS_SELECTOR, "#tbl tbody tr")) > 25
    WebDriverWait(driver, timeout).until(all_shown)
    time.sleep(1)


def filter_table(driver, text):
    box = driver.find_element(By.CSS_SELECTOR, "#tbl_filter input[type='search']")
    box.clear()
    box.send_keys(text)
    time.sleep(1.5)


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # older Pillow
        return ImageFont.load_default()


def add_banner(img_path: Path, lines):
    img = Image.open(img_path).convert("RGB")
    font = _font(max(14, img.width // 80))
    line_h = getattr(font, "size", 14) + 8
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?", default=DEFAULT_NAME, help="University name to search")
    ap.add_argument("-o", "--outdir", default="screenshots")
    ap.add_argument("--url", default=PAGE_URL)
    ap.add_argument("--category", choices=list(CATEGORY_BUTTONS), default="all",
                    help="Which button to click (default: all = 'View All')")
    ap.add_argument("--timeout", type=int, default=60, help="Seconds to wait for the table")
    ap.add_argument("--width", type=int, default=1700)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--no-filter", action="store_true",
                    help="Don't type the name into the Search box; keep the full list")
    ap.add_argument("--full-page", action="store_true", help="Capture the whole page height")
    ap.add_argument("--no-banner", action="store_true")
    ap.add_argument("--show-browser", action="store_true", help="Run non-headless (debugging)")
    ap.add_argument("--debug", action="store_true", help="Save screenshot + HTML on failure")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", args.name).strip("_")[:60]
    now = datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")

    driver = build_driver(args.width, args.height, headless=not args.show_browser)
    try:
        driver.get(args.url)
        try:
            wait_table_loaded(driver, args.timeout)
            click_category(driver, args.category, args.timeout)   # 1. View All
            show_all_entries(driver, args.timeout)                # 2. Show entries = All
        except TimeoutException:
            print("ERROR: the table did not load / switch in time "
                  "(site slow, blocked, or the page layout changed).")
            if args.debug:
                driver.save_screenshot(str(outdir / f"timeout_{stamp}.png"))
                (outdir / f"timeout_{stamp}.html").write_text(driver.page_source, encoding="utf-8")
            sys.exit(2)

        total = info_text(driver)
        rows = driver.execute_script(FIND_JS, args.name)         # 3. search all rows
        if not rows:
            print(f"NOT FOUND: '{args.name}' is not in the UGC list ({total}).")
            if args.debug:
                driver.save_screenshot(str(outdir / f"notfound_{slug}_{stamp}.png"))
                (outdir / f"notfound_{stamp}.html").write_text(driver.page_source, encoding="utf-8")
            sys.exit(1)

        # Optional: filter the table down to the matched row for a clean screenshot
        if not args.no_filter:
            filter_table(driver, rows[0][2])                     # exact name as listed on UGC

        driver.execute_script(HIGHLIGHT_JS, args.name, args.no_filter)  # 4. highlight
        time.sleep(0.4)

        if args.full_page:
            height = driver.execute_script("return document.documentElement.scrollHeight")
            driver.set_window_size(args.width, min(int(height) + 100, 16000))
            time.sleep(0.5)

        out = outdir / f"{slug}_{stamp}.png"
        driver.save_screenshot(str(out))                         # 5. screenshot

        if not args.no_banner:
            add_banner(out, [
                f"Source: {args.url}",
                f"Searched: {args.name}   |   Selection: {args.category}, entries = All",
                f"Captured: {now.strftime('%d %b %Y, %H:%M:%S')}",
            ])

        print(f"FOUND ({len(rows)} match(es)) -> {out}")
        for sr, typ, name, state, status in rows:
            print(f"  #{sr} | {typ} | {name} | {state} | {status}")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()