"""Extract application metadata from Village of Woodbury ZBA minutes PDFs.

Output: applications.json with one record per application found.
Records that can't be confidently parsed are still emitted with partial fields
so we can triage in the viewer.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import pdfplumber

PDF_ROOT = Path(__file__).parent / "pdfs" / "Zoning_Board"
OUTPUT = Path(__file__).parent / "applications.json"

MEETING_DATE_RE = re.compile(
    r"held on\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)

# Application section headers. Variants across years:
#   "A. Name –"     (2024 format with dash)
#   "A. Name"       (2024-08 format, no dash)
#   "A.Woodbury..." (2025 format, no space)
#   "a. Name -"     (2025 Action on Decisions, lowercase)
APP_HEADER_RE = re.compile(
    r"^\s*([A-Za-z])[\.\)]\s*([^\n]+?)\s*$",
    re.MULTILINE,
)

# Sections that look like headers but are actually agenda items, not applications
NON_APP_NAMES = (
    "executive session", "approval and acceptance", "new business",
    "public hearings", "action on decisions", "actions on decisions",
    "adjournment", "old business", "correspondence", "other business",
    "board member comment", "public comment",
)

TAX_MAP_RE = re.compile(
    r"Section\s+(\d+),?\s+Block\s+(\d+),?\s+Lot(?:s)?\s+([\d\.\&\s,and]+?)(?:[\.\s]|$)",
    re.IGNORECASE,
)

ZONING_DISTRICT_RE = re.compile(
    r"(?:located in the|in the)\s+([A-Z][A-Z0-9\-]{1,6})\s+Zoning District",
    re.IGNORECASE,
)

# Addresses: captures "at 58 Quaker Road" / "at 26 Stainton Fareway" / "along Valley Avenue"
STREET_SUFFIXES = (
    r"(?:Road|Rd|Drive|Dr|Avenue|Ave|Lane|Ln|Court|Ct|Street|St|"
    r"Boulevard|Blvd|Way|Place|Pl|Fareway|Circle|Cir|Terrace|Ter|"
    r"Hollow|Parkway|Pkwy|Trail|Tr|Highway|Hwy|Ridge|Hill)"
)
ADDRESS_RE = re.compile(
    rf"(?:located(?:[^.]*?)(?:at|along)\s+)"
    rf"(\d+\s+[A-Z][A-Za-z0-9\.\- ]+?\s+{STREET_SUFFIXES}"
    rf"|[A-Z][A-Za-z\.\- ]+?\s+{STREET_SUFFIXES})",
    re.IGNORECASE,
)

# Locality: which hamlet is it in?
LOCALITY_RE = re.compile(
    r"\b(Highland Mills|Central Valley|Woodbury)\b",
    re.IGNORECASE,
)

VARIANCE_TYPE_RE = re.compile(
    r"(?:requesting|appealing|requires?)\s+(?:an?\s+)?"
    r"(area variance|use variance|variance|special (?:use )?permit|"
    r"interpretation|appeal|determination)",
    re.IGNORECASE,
)


@dataclass
class Application:
    source_pdf: str
    meeting_date: str | None
    year: int | None
    label: str  # "A", "B", etc.
    name: str
    zoning_district: str | None = None
    street: str | None = None
    locality: str | None = None
    tax_map: str | None = None
    request_type: str | None = None
    raw_block: str = ""
    parse_warnings: list[str] = field(default_factory=list)


def parse_meeting_date(text: str) -> tuple[str | None, int | None]:
    m = MEETING_DATE_RE.search(text)
    if not m:
        return None, None
    # Normalize "April 10, 2024" or "April 10 2024"
    raw = m.group(1).replace(",", "")
    parts = raw.split()
    if len(parts) != 3:
        return raw, None
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
        "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
        "november": 11, "december": 12,
    }
    try:
        mo = months[parts[0].lower()]
        d = int(parts[1])
        y = int(parts[2])
        return date(y, mo, d).isoformat(), y
    except (KeyError, ValueError):
        return raw, None


def split_applications(text: str) -> list[tuple[str, str, str]]:
    """Return list of (label, name, body_text) for each A./B./C. section.

    Filters aggressively — ZBA minutes have many letter-labeled lines that
    aren't applications (agenda items, numbered lists in discussion, etc.).
    """
    headers = list(APP_HEADER_RE.finditer(text))
    out = []
    for i, m in enumerate(headers):
        label = m.group(1).upper()
        raw_name = m.group(2).strip()
        # Clip name at first dash/emdash/period-space; avoid capturing whole paragraph
        name = re.split(r"\s+[\-–—]\s+|\.\s+[A-Z]", raw_name, maxsplit=1)[0].strip(" -–—.")

        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[start:end].strip()

        lower = name.lower()
        if any(lower.startswith(x) for x in NON_APP_NAMES):
            continue
        # Body too short — likely a throwaway label
        if len(body) < 80:
            continue
        # Name sanity
        if not name or len(name) > 140:
            continue
        if re.match(r"^(the|a motion|motion|adopted|ayes|noes|absent)", lower):
            continue
        # Dialog attribution like "J. DeVenuto asked..." — reject if the name
        # line reads like someone speaking
        if re.search(
            r"\b(stated|asked|noted|continued|replied|responded|"
            r"inquired|explained|mentioned|commented|added)\b",
            raw_name, re.IGNORECASE,
        ):
            continue
        # Require the first ~600 chars of body to mention one of the strong
        # application markers. This filters out dialog-run-on false positives.
        head = body[:600]
        if not re.search(
            r"(public hearing|said property|zoning district|"
            r"tax map|section\s+\d+,?\s+block\s+\d+)",
            head, re.IGNORECASE,
        ):
            continue
        out.append((label, name, body))
    return out


def extract_fields(body: str) -> dict:
    d: dict = {}
    if m := ZONING_DISTRICT_RE.search(body):
        d["zoning_district"] = m.group(1).upper()
    if m := ADDRESS_RE.search(body):
        street = m.group(1).strip().rstrip(",.")
        d["street"] = re.sub(r"\s+", " ", street)
    if m := LOCALITY_RE.search(body):
        d["locality"] = m.group(1).title()
    if m := TAX_MAP_RE.search(body):
        lots = re.sub(r"\s+", " ", m.group(3)).strip(", ")
        d["tax_map"] = f"{m.group(1)}-{m.group(2)}-{lots}"
    if m := VARIANCE_TYPE_RE.search(body):
        d["request_type"] = m.group(1).lower()
    return d


def process_pdf(path: Path) -> list[Application]:
    try:
        with pdfplumber.open(path) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
    except Exception as e:
        print(f"  FAILED open: {path.name}: {e}")
        return []
    text = "\n".join(pages)

    meeting_date, year = parse_meeting_date(text)
    apps = []
    for label, name, body in split_applications(text):
        fields = extract_fields(body)
        warnings = []
        if "street" not in fields and "tax_map" not in fields:
            warnings.append("no_location")
        app = Application(
            source_pdf=str(path.relative_to(PDF_ROOT.parent.parent)),
            meeting_date=meeting_date,
            year=year,
            label=label,
            name=name[:200],
            raw_block=body[:2000],
            parse_warnings=warnings,
            **fields,
        )
        apps.append(app)
    return apps


def main():
    pdfs = sorted(PDF_ROOT.rglob("*.pdf"))
    print(f"Found {len(pdfs)} ZBA PDFs")
    all_apps: list[Application] = []
    n_with_loc = 0
    for path in pdfs:
        apps = process_pdf(path)
        for a in apps:
            if a.street or a.tax_map:
                n_with_loc += 1
        all_apps.extend(apps)
        print(f"  {path.name}: {len(apps)} applications "
              f"(meeting {apps[0].meeting_date if apps else 'unknown'})")

    OUTPUT.write_text(json.dumps([asdict(a) for a in all_apps], indent=2))
    print(f"\nWrote {len(all_apps)} applications to {OUTPUT}")
    print(f"  {n_with_loc} have a street or tax map reference "
          f"({100*n_with_loc/max(len(all_apps),1):.0f}%)")


if __name__ == "__main__":
    main()
