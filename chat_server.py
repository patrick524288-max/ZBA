"""HTTP server: serves the static viewer AND a /chat endpoint backed by Claude.

Replaces `python -m http.server`. Run with:

    export ANTHROPIC_API_KEY=sk-ant-...
    .venv/bin/python chat_server.py

Design:
- Retrieval is keyword/entity/address matching over applications_geocoded.json.
  Top N matches are bundled into the prompt as context.
- Claude call uses adaptive thinking and streams server-side to avoid HTTP
  timeouts on long responses; the full message is returned as JSON to the
  browser in one shot (v1; real-time streaming to the browser is v2).
- System prompt is cached (prefix-stable); per-query context is volatile and
  comes after the cache breakpoint.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import anthropic
import requests

import municipality

ROOT = Path(__file__).parent
ANNOTATIONS_PATH = municipality.shared_path("annotations.json")
_annotations_lock = threading.Lock()

_MUNI_RE = re.compile(r"^/m/([a-z0-9][a-z0-9-]{0,63})(/.*)?$")


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — no external dep. Values already in env are not overwritten."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(ROOT / ".env")

PORT = int(os.environ.get("PORT", "8765"))

# Per-municipality apps, loaded lazily on first chat call.
_apps_cache: dict[str, list[dict]] = {}


def get_apps(slug: str) -> list[dict]:
    if slug not in _apps_cache:
        path = municipality.derived_dir(slug) / "applications_geocoded.json"
        _apps_cache[slug] = json.loads(path.read_text()) if path.exists() else []
    return _apps_cache[slug]

# --- Retrieval -------------------------------------------------------------

STREET_SUFFIXES = (
    r"road|rd|drive|dr|avenue|ave|lane|ln|court|ct|street|st|"
    r"boulevard|blvd|way|place|pl|fareway|circle|cir|terrace|ter|"
    r"hollow|parkway|pkwy|trail|tr"
)
STREET_RE = re.compile(
    rf"\b(?:\d+\s+)?([a-z][a-z\s]+?)\s+({STREET_SUFFIXES})\b",
    re.IGNORECASE,
)

# Entities worth boosting when mentioned by name. Add more over time.
KNOWN_ENTITIES = {
    "hartman", "clovewood", "rushmore", "giacomazza", "morgante",
    "barshov", "naughton", "moran", "panella", "gerver", "brady",
    "aeonn", "crystal springs", "woodbury villas", "kenny building",
    "valley seafood", "dougherty", "ungerer", "devenuto",
    "all mine", "woodbury common",
}
LOCALITIES = ("highland mills", "central valley", "woodbury")
STOP = {
    "what", "when", "where", "which", "whose", "have", "happened", "happen",
    "there", "street", "road", "drive", "lane", "avenue", "this", "that",
    "with", "been", "does", "doing", "about", "anything", "something",
    "these", "those", "from", "into", "near", "around", "years", "year",
}


def retrieve(question: str, apps: list[dict], n: int = 20) -> list[dict]:
    q = question.lower()
    streets = [m.group(0).strip().lower() for m in STREET_RE.finditer(q)]
    years = set(re.findall(r"\b(20[0-3]\d)\b", q))
    localities = [l for l in LOCALITIES if l in q]
    entities = [e for e in KNOWN_ENTITIES if e in q]
    tokens = {t for t in re.findall(r"\w{4,}", q) if t not in STOP and not t.isdigit()}

    scored = []
    for a in apps:
        score = 0
        bl = (a.get("raw_block") or "").lower()
        name = (a.get("name") or "").lower()
        street = (a.get("street") or "").lower()
        loc = (a.get("locality") or "").lower()

        for s in streets:
            if s and street and (s in street or street in s):
                score += 15
        for e in entities:
            if e in name or e in bl:
                score += 10
        if str(a.get("year")) in years:
            score += 3
        for l in localities:
            if l in loc:
                score += 2
        hits = sum(1 for t in tokens if t in bl or t in name)
        score += min(hits, 5)

        if score > 0:
            scored.append((score, a))

    scored.sort(key=lambda x: -x[0])
    return [a for _, a in scored[:n]]


# --- Claude call -----------------------------------------------------------

BOARD_NAMES = {"ZBA": "Zoning Board of Appeals", "PB": "Planning Board"}

SYSTEM_PROMPT = """You analyze meeting minutes from the Village of Woodbury, NY boards — Planning Board (PB), Zoning Board of Appeals (ZBA), and Village Board of Trustees.

You answer questions using ONLY the retrieved application records provided as context on each turn. You do not make claims beyond what those records say.

## Hard rules
1. Facts with citations, never accusations. Public-record facts — applications, addresses, dates, decisions, quoted statements — are fair game. Do not speculate about motives, corruption, or conspiracies even when the user invites it. Do not make defamatory claims about named individuals.
2. Cite every substantive claim inline with this format: [BOARD YYYY-MM-DD: applicant name]. Example: [ZBA 2024-04-10: 58 Quaker].
3. If the retrieved records don't contain what the user asked about, say so plainly. Do not invent records, dates, or applicants.
4. Distinguish clearly:
   - Confirmed: stated directly in a specific meeting's minutes
   - Pattern: observed across multiple records (say how many)
   - Gap: user's question asks about something not in the retrieved records
5. If the user's question is vague (e.g. "my street" without naming a street), ask them to specify before answering.
6. Keep answers terse. 3-8 sentences is usually right. Use bullets only when listing 3+ distinct items.

## Output style
- Write for a reader who can't see the map or the raw records. Self-contained prose with citations.
- No preambles ("Great question!"), no summaries of the summary, no closing offers to help further.
- If you spot something unusual or noteworthy in the records (same applicant across many years, semantic reframing of a project, a parcel bouncing between boards), mention it in one sentence — flagged as a pattern, not a conclusion."""


def format_context(matches: list[dict]) -> str:
    if not matches:
        return "(No applications matched this query. Tell the user nothing relevant was found in the retrieved corpus.)"
    parts = []
    for a in matches:
        board = a.get("board", "?")
        date = a.get("meeting_date") or f"{a.get('year') or '????'}-??-??"
        parts.append(
            f"--- [{board} {date}] {a.get('name', '(unnamed)')} ---\n"
            f"Address: {a.get('street') or '—'} ({a.get('locality') or '—'})\n"
            f"Tax map: {a.get('tax_map') or '—'} | Zoning: {a.get('zoning_district') or '—'} | Request: {a.get('request_type') or '—'}\n"
            f"Source PDF: {a.get('source_pdf')}\n"
            f"Excerpt: {a.get('raw_block', '')[:1500]}"
        )
    return "\n\n".join(parts)


def ask_claude(client: anthropic.Anthropic, question: str, matches: list[dict]) -> str:
    context = format_context(matches)
    user = f"## Retrieved records:\n\n{context}\n\n## User question:\n{question}"

    with client.messages.stream(
        model="claude-opus-4-7",
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user}],
    ) as stream:
        final = stream.get_final_message()

    for block in final.content:
        if block.type == "text":
            return block.text
    return "(Claude returned no text content.)"


def slim_citation(a: dict) -> dict:
    return {
        "board": a.get("board"),
        "meeting_date": a.get("meeting_date"),
        "year": a.get("year"),
        "name": a.get("name"),
        "street": a.get("street"),
        "locality": a.get("locality"),
        "tax_map": a.get("tax_map"),
        "source_pdf": a.get("source_pdf"),
        "lat": a.get("lat"),
        "lon": a.get("lon"),
    }


# --- Annotations (user-added pins) ----------------------------------------

WOODBURY_BBOX = "-74.200,41.280,-74.050,41.420"
GEOCODER_UA = "WoodburyZoningViewer/0.2 (annotations)"


_LEGACY_DATE_FORMATS = (
    "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
    "%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%Y/%m/%d",
)


def _coerce_date(raw: str) -> tuple[str, int] | None:
    """Return (iso_date, year) for a date string in ISO or common US formats."""
    if not raw:
        return None
    raw = raw.strip()
    # Fast path: already ISO
    try:
        d = datetime.fromisoformat(raw.split("T")[0])
        return d.date().isoformat(), d.year
    except ValueError:
        pass
    for fmt in _LEGACY_DATE_FORMATS:
        try:
            d = datetime.strptime(raw, fmt)
            return d.date().isoformat(), d.year
        except ValueError:
            continue
    return None


def _load_annotations() -> list[dict]:
    if not ANNOTATIONS_PATH.exists():
        return []
    try:
        items = json.loads(ANNOTATIONS_PATH.read_text())
    except json.JSONDecodeError:
        return []
    # One-shot migration: parse any free-form dates into ISO + year
    dirty = False
    for a in items:
        if "year" in a and a.get("year") is not None:
            continue
        parsed = _coerce_date(a.get("date") or "")
        if parsed:
            a["date"], a["year"] = parsed
            dirty = True
    if dirty:
        _save_annotations(items)
    return items


def _save_annotations(items: list[dict]) -> None:
    tmp = ANNOTATIONS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2))
    tmp.replace(ANNOTATIONS_PATH)


def _geocode(address: str) -> dict | None:
    """Return {lat, lon, display_name} or None. Biased to Woodbury."""
    params = {
        "q": address,
        "format": "json",
        "limit": 3,
        "viewbox": WOODBURY_BBOX,
        "bounded": 0,
        "countrycodes": "us",
    }
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params=params,
        headers={"User-Agent": GEOCODER_UA},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    # Prefer a hit that mentions Woodbury/Orange County; else first result
    for hit in data:
        dn = (hit.get("display_name") or "").lower()
        if "orange county" in dn or "woodbury" in dn:
            return {"lat": float(hit["lat"]), "lon": float(hit["lon"]),
                    "display_name": hit.get("display_name")}
    hit = data[0]
    return {"lat": float(hit["lat"]), "lon": float(hit["lon"]),
            "display_name": hit.get("display_name")}


# --- HTTP handler ----------------------------------------------------------

client: anthropic.Anthropic | None = None


class Handler(SimpleHTTPRequestHandler):
    # Suppress noisy default logging of every static request
    def log_message(self, fmt, *args):
        if self.path == "/chat" or "error" in fmt.lower():
            sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/annotations":
            with _annotations_lock:
                return self._json({"annotations": _load_annotations()})

        # Legacy top-level HTML → redirect into /m/<slug>/ so relative data URLs resolve
        if path in ("/", "/index.html", "/trends.html"):
            slugs = municipality.list_slugs()
            if slugs:
                slug = slugs[0]
                page = path.lstrip("/") or ""
                target = f"/m/{slug}/{page}"
                self.send_response(302)
                self.send_header("Location", target)
                self.end_headers()
                return

        # /m/<slug>/... — rewrite to actual file path and delegate
        m = _MUNI_RE.match(path)
        if m:
            rewritten = self._rewrite_muni_path(m.group(1), m.group(2) or "/")
            if rewritten is None:
                return self.send_error(404)
            self.path = rewritten + (("?" + urlparse(self.path).query) if urlparse(self.path).query else "")
            return super().do_GET()

        return super().do_GET()

    def _rewrite_muni_path(self, slug: str, rest: str) -> str | None:
        """Map /m/<slug>/<rest> to an actual file path under the project root.
        Returns None if the file doesn't exist."""
        rest = rest.lstrip("/")
        if rest in ("", "index.html"):
            return "/index.html"
        if rest == "trends.html":
            return "/trends.html"
        # Derived (JSON) first, then assets (PNG/etc.)
        for subdir in ("derived", "assets"):
            p = municipality.municipality_dir(slug) / subdir / rest
            try:
                p.resolve().relative_to((municipality.municipality_dir(slug) / subdir).resolve())
            except ValueError:
                return None  # path traversal attempt
            if p.is_file():
                return "/" + str(p.relative_to(ROOT))
        return None

    def do_PUT(self):
        path = urlparse(self.path).path
        q = urlparse(self.path).query
        if path != "/annotations":
            return self.send_error(404)
        params = dict(p.split("=", 1) for p in q.split("&") if "=" in p)
        ann_id = params.get("id", "").strip()
        if not ann_id:
            return self._json({"error": "missing id"}, 400)

        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return self._json({"error": "invalid JSON"}, 400)

        address = (body.get("address") or "").strip()
        description = (body.get("description") or "").strip()
        date_raw = (body.get("date") or "").strip()
        if not address:
            return self._json({"error": "address required"}, 400)
        if not date_raw:
            return self._json({"error": "date required"}, 400)
        parsed = _coerce_date(date_raw)
        if not parsed:
            return self._json({"error": "date must be YYYY-MM-DD"}, 400)
        iso_date, year = parsed
        if len(address) > 300 or len(description) > 1000:
            return self._json({"error": "field too long"}, 400)

        with _annotations_lock:
            items = _load_annotations()
            idx = next((i for i, a in enumerate(items) if a.get("id") == ann_id), -1)
            if idx == -1:
                return self._json({"error": "not found"}, 404)
            current = dict(items[idx])
            if address != current.get("address"):
                try:
                    geo = _geocode(address)
                except requests.RequestException as e:
                    return self._json({"error": f"geocoder error: {e}"}, 502)
                if not geo:
                    return self._json({"error": "address not found"}, 404)
                current["lat"] = geo["lat"]
                current["lon"] = geo["lon"]
                current["display_name"] = geo["display_name"]
            current["address"] = address
            current["description"] = description
            current["date"] = iso_date
            current["year"] = year
            current["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            items[idx] = current
            _save_annotations(items)
        return self._json({"annotation": current})

    def do_DELETE(self):
        path = urlparse(self.path).path
        q = urlparse(self.path).query
        if path != "/annotations":
            return self.send_error(404)
        # Parse id from query string
        params = dict(p.split("=", 1) for p in q.split("&") if "=" in p)
        ann_id = params.get("id", "").strip()
        if not ann_id:
            return self._json({"error": "missing id"}, 400)
        with _annotations_lock:
            items = _load_annotations()
            before = len(items)
            items = [a for a in items if a.get("id") != ann_id]
            if len(items) == before:
                return self._json({"error": "not found"}, 404)
            _save_annotations(items)
        return self._json({"ok": True, "id": ann_id})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/annotations":
            return self._handle_annotation_create()
        # Per-municipality chat: /m/<slug>/chat
        m = _MUNI_RE.match(path)
        slug = None
        if m and (m.group(2) or "") == "/chat":
            slug = m.group(1)
        elif path == "/chat":
            # Backwards compat: default to the one municipality we have
            slugs = municipality.list_slugs()
            slug = slugs[0] if slugs else None
        if not slug:
            self.send_error(404, "Unknown endpoint")
            return

        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return self._json({"error": "invalid JSON"}, 400)

        question = (body.get("question") or "").strip()
        if not question:
            return self._json({"error": "empty question"}, 400)
        if len(question) > 2000:
            return self._json({"error": "question too long (max 2000 chars)"}, 400)

        if client is None:
            return self._json({"error": "ANTHROPIC_API_KEY not set on server"}, 500)

        try:
            apps = get_apps(slug)
            matches = retrieve(question, apps)
            answer = ask_claude(client, question, matches)
            self._json({
                "answer": answer,
                "citations": [slim_citation(m) for m in matches[:10]],
                "n_retrieved": len(matches),
            })
        except anthropic.AuthenticationError:
            self._json({"error": "Claude API key invalid or missing"}, 500)
        except anthropic.RateLimitError:
            self._json({"error": "Rate limited — retry in a moment"}, 429)
        except anthropic.APIError as e:
            self._json({"error": f"Claude API error: {e.message}"}, 502)
        except Exception as e:
            sys.stderr.write(f"chat error: {type(e).__name__}: {e}\n")
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_annotation_create(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return self._json({"error": "invalid JSON"}, 400)
        address = (body.get("address") or "").strip()
        description = (body.get("description") or "").strip()
        date_raw = (body.get("date") or "").strip()
        if not address:
            return self._json({"error": "address required"}, 400)
        if not date_raw:
            return self._json({"error": "date required"}, 400)
        parsed = _coerce_date(date_raw)
        if not parsed:
            return self._json({"error": "date must be YYYY-MM-DD"}, 400)
        iso_date, year = parsed
        if len(address) > 300 or len(description) > 1000:
            return self._json({"error": "field too long"}, 400)
        try:
            geo = _geocode(address)
        except requests.RequestException as e:
            return self._json({"error": f"geocoder error: {e}"}, 502)
        if not geo:
            return self._json({"error": "address not found"}, 404)

        ann = {
            "id": secrets.token_urlsafe(8),
            "address": address,
            "description": description,
            "date": iso_date,
            "year": year,
            "lat": geo["lat"],
            "lon": geo["lon"],
            "display_name": geo["display_name"],
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with _annotations_lock:
            items = _load_annotations()
            items.append(ann)
            _save_annotations(items)
        self._json({"annotation": ann})

    def _json(self, obj: dict, code: int = 200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    global client
    try:
        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        # Sanity: try a 1-token count to verify auth wiring (cheap, offline)
    except Exception as e:
        sys.stderr.write(f"WARNING: Claude client init failed ({e}). /chat will return errors.\n")
        client = None

    slugs = municipality.list_slugs()
    print(f"Serving http://localhost:{PORT}  ({len(slugs)} municipalities: {', '.join(slugs) or '—'})")
    print(f"Chat: {'ENABLED' if client else 'DISABLED (set ANTHROPIC_API_KEY)'}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
