"""Scrape the Village of Woodbury document center to build filename → public URL map.

Output: pdf_urls.json — `{pdf_basename: full_url}` for every ZBA and PB
meeting minutes PDF the site publishes.

The site 403s on naked requests; a Referer from the parent directory page
plus a cookie jar is enough to pass.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import requests

import municipality

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


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


def scrape_joomla_docman(session: requests.Session, base_url: str,
                         board_roots: dict[str, str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for board, root_path in board_roots.items():
        root_url = base_url + root_path
        print(f"\n=== {board} ===")
        root_html = fetch(session, root_url, base_url + "/")
        year_urls = sorted(set(re.findall(YEAR_LINK_RE, root_html)))
        print(f"  {len(year_urls)} year pages")
        for year_path in year_urls:
            year_url = base_url + year_path
            try:
                html = fetch(session, year_url, root_url)
            except Exception as e:
                print(f"  ERR {year_path}: {e}")
                continue
            matches = FILE_LINK_RE.findall(html)
            for path, slug_with_id in matches:
                m = re.match(r"^\d+-(.+)$", slug_with_id)
                if not m:
                    continue
                mapping[m.group(1)] = base_url + path
            print(f"  {year_path.split('/')[-1]}: {len(matches)} files")
            time.sleep(0.4)  # be polite
    return mapping


ADAPTERS = {
    "joomla-docman": scrape_joomla_docman,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", "-m", default=municipality.DEFAULT_SLUG)
    args = ap.parse_args()
    cfg = municipality.load_config(args.slug)
    dc = cfg.get("doc_center", {})
    adapter_name = dc.get("adapter")
    adapter = ADAPTERS.get(adapter_name)
    if not adapter:
        raise SystemExit(f"No scraper adapter named {adapter_name!r} (have {list(ADAPTERS)})")

    session = requests.Session()
    mapping = adapter(session, dc["base_url"], dc["board_roots"])

    out = municipality.derived_dir(args.slug) / "pdf_urls.json"
    out.write_text(json.dumps(mapping, indent=2, sort_keys=True))
    print(f"\nTotal: {len(mapping)} unique slugs")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
