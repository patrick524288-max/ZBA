"""Scrape the Village of Woodbury document center to build filename → public URL map.

Output: pdf_urls.json — `{pdf_basename: full_url}` for every ZBA and PB
meeting minutes PDF the site publishes.

The site 403s on naked requests; a Referer from the parent directory page
plus a cookie jar is enough to pass.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

BASE = "https://villageofwoodbury.com"
ROOTS = {
    "ZBA": f"{BASE}/document-center/agendas-minutes-meeting-documents/zoning-board-of-appeals-minutes.html",
    "PB":  f"{BASE}/document-center/agendas-minutes-meeting-documents/planning-board-minutes.html",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

OUT = Path(__file__).parent / "pdf_urls.json"


def fetch(session: requests.Session, url: str, referer: str) -> str:
    r = session.get(url, headers={**HEADERS, "Referer": referer}, timeout=30)
    r.raise_for_status()
    return r.text


YEAR_LINK_RE = re.compile(
    r'href="(/document-center/agendas-minutes-meeting-documents/[^"]+?/20\d{2}[^"]*?\.html)"'
)
FILE_LINK_RE = re.compile(
    r'href="(/document-center/agendas-minutes-meeting-documents/[^"]+?/(\d+-[a-z0-9\-]+?)/file\.html)"'
)


def main():
    session = requests.Session()
    mapping: dict[str, str] = {}
    by_board: dict[str, dict[str, str]] = {"ZBA": {}, "PB": {}}

    for board, root_url in ROOTS.items():
        print(f"\n=== {board} ===")
        # Get root → list of year pages
        root_html = fetch(session, root_url, BASE + "/")
        year_urls = sorted(set(re.findall(YEAR_LINK_RE, root_html)))
        print(f"  {len(year_urls)} year pages")

        for year_path in year_urls:
            year_url = BASE + year_path
            try:
                html = fetch(session, year_url, root_url)
            except Exception as e:
                print(f"  ERR {year_path}: {e}")
                continue
            matches = FILE_LINK_RE.findall(html)
            for path, slug_with_id in matches:
                # slug_with_id is like "4630-zba-2024-0110-minutes"
                # Strip the leading numeric ID to get a filename-like key
                m = re.match(r"^\d+-(.+)$", slug_with_id)
                if not m:
                    continue
                slug = m.group(1)  # "zba-2024-0110-minutes"
                url = BASE + path
                mapping[slug] = url
                by_board[board][slug] = url
            print(f"  {year_path.split('/')[-1]}: {len(matches)} files")
            time.sleep(0.4)  # be polite

    print(f"\nTotal: {len(mapping)} unique slugs")
    print(f"  ZBA: {len(by_board['ZBA'])}, PB: {len(by_board['PB'])}")

    OUT.write_text(json.dumps(mapping, indent=2, sort_keys=True))
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
