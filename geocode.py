"""Geocode application street addresses via Nominatim (OpenStreetMap).

Nominatim's public instance is rate-limited to ~1 req/sec with a descriptive
User-Agent. We cache results in geocode_cache.json so reruns don't re-hit.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
APPS = ROOT / "applications.json"
CACHE = ROOT / "geocode_cache.json"
OUTPUT = ROOT / "applications_geocoded.json"

# Village of Woodbury encompasses these hamlets. Most addresses are in one of them.
# We bias the query geographically via a viewbox around Woodbury NY.
WOODBURY_BBOX = "-74.200,41.280,-74.050,41.420"  # left,bottom,right,top (lon,lat)

USER_AGENT = "WoodburyZoningViewer/0.1 (MVP research tool)"


def load_cache() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text())
    return {}


def save_cache(cache: dict) -> None:
    CACHE.write_text(json.dumps(cache, indent=2))


def build_query(app: dict) -> str | None:
    street = app.get("street")
    if not street:
        return None
    locality = app.get("locality") or "Woodbury"
    # Highland Mills and Central Valley are both hamlets within the Village/Town of Woodbury
    return f"{street}, {locality}, NY"


def _nominatim_search(query: str, bounded: int, session: requests.Session):
    params = {
        "q": query,
        "format": "json",
        "limit": 3,
        "viewbox": WOODBURY_BBOX,
        "bounded": bounded,
        "countrycodes": "us",
    }
    r = session.get(
        "https://nominatim.openstreetmap.org/search",
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _pick(hits: list, require_orange_county: bool = False):
    """Pick the best hit; optionally require Orange County to avoid wild matches."""
    for h in hits:
        display = (h.get("display_name") or "").lower()
        if require_orange_county and "orange county" not in display:
            continue
        if "new york" not in display and "ny" not in display:
            continue
        return {
            "lat": float(h["lat"]),
            "lon": float(h["lon"]),
            "display_name": h.get("display_name"),
        }
    return None


def geocode(query: str, session: requests.Session) -> dict | None:
    # Skip obviously bad queries (regex noise)
    lower = query.lower()
    if any(b in lower for b in ("corner of", "entrance to", "along ")):
        return None

    # Try 1: strict viewbox around Woodbury
    hits = _nominatim_search(query, bounded=1, session=session)
    if hit := _pick(hits):
        return hit

    # Try 2: drop locality, search broader Orange County
    time.sleep(1.1)
    street = query.split(",")[0].strip()
    broad_q = f"{street}, Orange County, NY"
    hits = _nominatim_search(broad_q, bounded=0, session=session)
    if hit := _pick(hits, require_orange_county=True):
        return hit

    return None


def main():
    apps = json.loads(APPS.read_text())
    cache = load_cache()
    session = requests.Session()

    unique_queries = sorted({build_query(a) for a in apps} - {None})
    print(f"{len(apps)} applications, {len(unique_queries)} unique addresses")

    new_lookups = 0
    for i, q in enumerate(unique_queries):
        if q in cache:
            continue
        try:
            cache[q] = geocode(q, session)
        except Exception as e:
            print(f"  ERR {q}: {e}")
            cache[q] = None
        new_lookups += 1
        if new_lookups % 10 == 0:
            print(f"  {new_lookups} geocoded... (latest: {q[:60]})")
            save_cache(cache)
        # Nominatim usage policy: max 1 req/sec
        time.sleep(1.1)
    save_cache(cache)
    print(f"Geocoded {new_lookups} new addresses (cache size: {len(cache)})")

    # Attach lat/lon to each application
    out = []
    n_mapped = 0
    for a in apps:
        q = build_query(a)
        geo = cache.get(q) if q else None
        if geo:
            a["lat"] = geo["lat"]
            a["lon"] = geo["lon"]
            a["geo_display_name"] = geo["display_name"]
            n_mapped += 1
        out.append(a)

    OUTPUT.write_text(json.dumps(out, indent=2))
    print(f"Wrote {OUTPUT} — {n_mapped}/{len(apps)} apps mapped "
          f"({100*n_mapped/len(apps):.0f}%)")


if __name__ == "__main__":
    main()
