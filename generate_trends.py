"""Ask Claude to write per-year trend summaries + a 2026 prediction.

Reads applications_geocoded.json, groups by year, builds per-year stats
and excerpts, sends to Claude Opus 4.7 with structured-output JSON. Saves
the result to trends.json for the static frontend to read.

Requires ANTHROPIC_API_KEY (loaded from .env the same way chat_server.py does).
"""
from __future__ import annotations

import json
import os
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

import anthropic

ROOT = Path(__file__).parent
APPS_PATH = ROOT / "applications_geocoded.json"
OUT = ROOT / "trends.json"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(ROOT / ".env")


# Entities worth surfacing when they show up frequently in a year (repeat players).
REPEAT_PLAYERS = [
    "hartman", "morgante", "barshov", "gelb", "clovewood", "rushmore",
    "crystal springs", "woodbury villas", "aeonn", "kenny building",
    "valley seafood", "woodbury common", "smith clove", "devenuto",
]


def year_bundle(apps_in_year: list[dict]) -> dict:
    total = len(apps_in_year)
    by_board = Counter(a.get("board") for a in apps_in_year)
    by_district = Counter(a.get("zoning_district") for a in apps_in_year if a.get("zoning_district"))
    by_request = Counter(a.get("request_type") for a in apps_in_year if a.get("request_type"))
    by_locality = Counter(a.get("locality") for a in apps_in_year if a.get("locality"))

    # Repeat-player mentions across raw_block text
    blob = " ".join((a.get("raw_block") or "").lower() for a in apps_in_year)
    player_hits = {p: blob.count(p) for p in REPEAT_PLAYERS if blob.count(p) >= 2}

    # Representative names (dedup by address+name for variety)
    seen = set()
    names_sample = []
    for a in apps_in_year:
        key = (a.get("name"), a.get("street"))
        if key in seen:
            continue
        seen.add(key)
        names_sample.append(a.get("name"))
        if len(names_sample) >= 20:
            break

    # Excerpts: pick up to 8 short samples, strip to ~220 chars of first sentence
    random.seed(42 + (apps_in_year[0].get("year") or 0))
    rand_apps = random.sample(apps_in_year, min(8, len(apps_in_year)))
    excerpts = []
    for a in rand_apps:
        raw = (a.get("raw_block") or "").strip()
        excerpt = re.sub(r"\s+", " ", raw)[:220]
        if excerpt:
            excerpts.append(f"[{a.get('board')} {a.get('meeting_date') or a.get('year')}] {a.get('name', '')[:40]}: {excerpt}")

    return {
        "total_applications": total,
        "by_board": dict(by_board),
        "top_zoning_districts": by_district.most_common(6),
        "top_request_types": by_request.most_common(6),
        "top_localities": by_locality.most_common(3),
        "repeat_player_mentions": player_hits,
        "sample_application_names": names_sample,
        "excerpts": excerpts,
    }


# Demographic-signal keywords. These are *application-text* terms — religious-use
# markers, household-adaptation markers, and regional community references —
# chosen because they appear verbatim in the minutes. We count their occurrences
# and pass the counts to Claude so the analysis is grounded in what the minutes
# literally say. NOT used to profile applicants or infer residents' identities.
DEMO_KEYWORDS = {
    "religious_use": [
        "mikvah", "mikveh", "shul", "yeshiva", "synagogue", "kosher",
        "place of worship", "place of assembly", "religious use",
    ],
    "cross_community": [
        "kiryas joel", "palm tree", "kj ", "satmar", "hasidic", "chasidic",
        "orthodox", "jewish community",
    ],
    "household_adaptation": [
        "second kitchen", "accessory dwelling", "accessory apartment",
        "multi-family", "multifamily", "two-family", "family compound",
        "in-law", "additional bedroom", "bungalow",
    ],
}


def demographic_bundle(apps_in_year: list[dict]) -> dict:
    blob = " ".join((a.get("raw_block") or "").lower() for a in apps_in_year)
    hits = {}
    matched_apps = {}  # category -> list of (name, street, excerpt)
    for category, terms in DEMO_KEYWORDS.items():
        cat_hits = {}
        for term in terms:
            count = blob.count(term)
            if count > 0:
                cat_hits[term] = count
        hits[category] = cat_hits

        samples = []
        for a in apps_in_year:
            rb = (a.get("raw_block") or "").lower()
            for term in terms:
                if term in rb:
                    idx = rb.find(term)
                    excerpt = re.sub(r"\s+", " ", rb[max(0, idx-60):idx+160])
                    samples.append(
                        f"[{a.get('board')} {a.get('meeting_date') or a.get('year')}] "
                        f"{(a.get('name') or '')[:50]} @ {a.get('street') or '—'} :: …{excerpt}…"
                    )
                    break
            if len(samples) >= 6:
                break
        matched_apps[category] = samples

    return {"keyword_hits": hits, "excerpts_by_category": matched_apps,
            "total_applications": len(apps_in_year)}


def build_prompt(year_bundles: dict[int, dict]) -> str:
    lines = []
    for yr in sorted(year_bundles):
        b = year_bundles[yr]
        lines.append(f"\n=== {yr} ({b['total_applications']} applications) ===")
        lines.append(f"By board: {b['by_board']}")
        lines.append(f"Top zoning districts: {b['top_zoning_districts']}")
        lines.append(f"Top request types: {b['top_request_types']}")
        lines.append(f"Top localities: {b['top_localities']}")
        if b["repeat_player_mentions"]:
            lines.append(f"Repeat-player mentions (name: hits): {b['repeat_player_mentions']}")
        lines.append(f"Application names sample: {b['sample_application_names'][:12]}")
        lines.append("Excerpts:")
        for e in b["excerpts"]:
            lines.append(f"  - {e}")
    return "\n".join(lines)


SYSTEM = """You analyze Village of Woodbury, NY board meeting minutes — Planning Board (PB) and Zoning Board of Appeals (ZBA) — across 12 years of applications.

For each year you're given, write 2–4 sentences capturing the DOMINANT TREND. The reader already sees the raw counts; your job is qualitative: what kind of year was it? What changed from the prior year? Is there a repeat applicant or LLC showing up a lot? Did zoning fights cluster in a particular district or code section?

After all years, write a 4–6 sentence PREDICTION for 2026 extrapolating from the 12-year trajectory.

Hard rules:
- Facts and observed patterns only. No accusations about named individuals. Public-record LLCs and their activity counts are fair game; motives and speculation about corruption are not.
- If a year's data is thin (fewer than ~15 applications), say so explicitly.
- Don't restate stats verbatim — interpret them.
- The 2026 prediction is "based on the trajectory, expect X" — cautious, not fortune-telling.

Return your answer as JSON matching the provided schema."""


SCHEMA = {
    "type": "object",
    "properties": {
        "years": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "year": {"type": "integer"},
                    "summary": {"type": "string"},
                },
                "required": ["year", "summary"],
                "additionalProperties": False,
            },
        },
        "prediction_2026": {"type": "string"},
    },
    "required": ["years", "prediction_2026"],
    "additionalProperties": False,
}


DEMO_SYSTEM = """You analyze demographic signals reflected in Village of Woodbury, NY board meeting minutes. The Village is directly adjacent to Palm Tree (formerly Kiryas Joel), a large Satmar Hasidic community, and regional news and court records (including the Palm Tree annexation litigation) document meaningful development-pressure spillover. Your job is to describe what the *application text* shows about this pattern across 12 years.

HARD RULES — read carefully:
1. Describe APPLICATION PATTERNS only. Never characterize communities, residents, religious groups, or people. "12 religious-use applications filed in 2022" is fine. "The community did X" is not.
2. Every claim must be traceable to observable application data — counts of religious-use filings, household-adaptation variances (accessory apartment, second kitchen, two-family conversion), cross-community references in the minutes (Palm Tree, Kiryas Joel, etc.), or specific application excerpts.
3. Use the minutes' own terminology. If a filing is a "place of worship" application, call it that. Avoid loaded framing.
4. Distinguish application data from resident demographics. A variance filing tells you about development, not about who lives there.
5. No speculation about motives, strategy, religious obligation, or community intent. If a pattern is observed, state the pattern — don't explain *why* people might be doing it.
6. Flag thin years honestly. If a year has fewer than ~5 demographic-coded filings, say the signal is weak.
7. Keep per-year summaries to 2–4 sentences. Concrete, grounded, neutral.

Start with a short framing paragraph (3–5 sentences) that: notes the Palm Tree adjacency context, names the categories you're tracking in the application text (religious-use markers, household-adaptation variances, explicit cross-community references), and states the data limitation (minutes show filings, not residents).

End with a 4–6 sentence 2026 outlook extrapolating from the trajectory of these signals.

Return JSON matching the provided schema."""


DEMO_SCHEMA = {
    "type": "object",
    "properties": {
        "framing": {"type": "string"},
        "years": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "year": {"type": "integer"},
                    "summary": {"type": "string"},
                },
                "required": ["year", "summary"],
                "additionalProperties": False,
            },
        },
        "outlook_2026": {"type": "string"},
    },
    "required": ["framing", "years", "outlook_2026"],
    "additionalProperties": False,
}


def build_demo_prompt(demo_bundles: dict[int, dict]) -> str:
    lines = [
        "Below is the per-year demographic-signal data extracted from the Village of Woodbury board minutes. Each year shows keyword hits across three categories (religious-use terms, cross-community references, household-adaptation terms) and up to ~18 excerpted application passages where those terms appeared. Counts are raw occurrences across all application text for that year."
    ]
    for yr in sorted(demo_bundles):
        b = demo_bundles[yr]
        lines.append(f"\n=== {yr} (total applications: {b['total_applications']}) ===")
        for cat, hits in b["keyword_hits"].items():
            if hits:
                lines.append(f"  {cat}: {hits}")
            else:
                lines.append(f"  {cat}: (no hits)")
        for cat, samples in b["excerpts_by_category"].items():
            for s in samples:
                lines.append(f"    - {s}")
    return "\n".join(lines)


def main():
    apps = json.loads(APPS_PATH.read_text())
    by_year = defaultdict(list)
    for a in apps:
        y = a.get("year")
        if y:
            by_year[y].append(a)

    year_bundles = {y: year_bundle(apps) for y, apps in by_year.items()}
    demo_bundles = {y: demographic_bundle(apps) for y, apps in by_year.items()}
    volume_prompt = build_prompt(year_bundles)
    demo_prompt = build_demo_prompt(demo_bundles)
    print(f"Volume prompt: {len(volume_prompt):,} chars")
    print(f"Demographics prompt: {len(demo_prompt):,} chars")

    client = anthropic.Anthropic()

    def call(system: str, user: str, schema: dict):
        with client.messages.stream(
            model="claude-opus-4-7",
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        ) as stream:
            msg = stream.get_final_message()
        txt = next((b.text for b in msg.content if b.type == "text"), "")
        return json.loads(txt), msg.usage

    print("→ Volume-trends call…")
    vol, vu = call(SYSTEM, volume_prompt, SCHEMA)
    print(f"   {vu.input_tokens} in, {vu.output_tokens} out")

    print("→ Demographics call…")
    demo, du = call(DEMO_SYSTEM, demo_prompt, DEMO_SCHEMA)
    print(f"   {du.input_tokens} in, {du.output_tokens} out")

    out = {
        "years": {str(item["year"]): item["summary"] for item in vol["years"]},
        "prediction_2026": vol["prediction_2026"],
        "demographics": {
            "framing": demo["framing"],
            "years": {str(item["year"]): item["summary"] for item in demo["years"]},
            "outlook_2026": demo["outlook_2026"],
            "keyword_hits_per_year": {str(y): b["keyword_hits"] for y, b in demo_bundles.items()},
        },
        "source_stats": {str(y): {k: v for k, v in b.items() if k != "excerpts"}
                         for y, b in year_bundles.items()},
        "total_applications": len(apps),
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(timespec="seconds"),
        "model": "claude-opus-4-7",
        "input_tokens": vu.input_tokens + du.input_tokens,
        "output_tokens": vu.output_tokens + du.output_tokens,
    }
    OUT.write_text(json.dumps(out, indent=2))
    print(f"Wrote {OUT} — total {out['input_tokens']} in, {out['output_tokens']} out")


if __name__ == "__main__":
    main()
