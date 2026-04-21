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
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anthropic

ROOT = Path(__file__).parent


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

APPS = json.loads((ROOT / "applications_geocoded.json").read_text())
PORT = int(os.environ.get("PORT", "8765"))

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


def retrieve(question: str, n: int = 20) -> list[dict]:
    q = question.lower()
    streets = [m.group(0).strip().lower() for m in STREET_RE.finditer(q)]
    years = set(re.findall(r"\b(20[0-3]\d)\b", q))
    localities = [l for l in LOCALITIES if l in q]
    entities = [e for e in KNOWN_ENTITIES if e in q]
    tokens = {t for t in re.findall(r"\w{4,}", q) if t not in STOP and not t.isdigit()}

    scored = []
    for a in APPS:
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


# --- HTTP handler ----------------------------------------------------------

client: anthropic.Anthropic | None = None


class Handler(SimpleHTTPRequestHandler):
    # Suppress noisy default logging of every static request
    def log_message(self, fmt, *args):
        if self.path == "/chat" or "error" in fmt.lower():
            sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    def do_POST(self):
        if self.path != "/chat":
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
            matches = retrieve(question)
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

    print(f"Serving http://localhost:{PORT}  ({len(APPS)} applications loaded)")
    print(f"Chat: {'ENABLED' if client else 'DISABLED (set ANTHROPIC_API_KEY)'}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
