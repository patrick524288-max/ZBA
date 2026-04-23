"""Geocode application street addresses via Nominatim (OpenStreetMap).

Nominatim's public instance is rate-limited to ~1 req/sec with a descriptive
User-Agent. We cache results in geocode_cache.json so reruns don't re-hit.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests

import municipality

USER_AGENT = "WoodburyZoningViewer/0.1 (MVP research tool)"


def load_cache(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_cache(cache: dict, path: Path) -> None:
    path.write_text(json.dumps(cache, indent=2))


def build_query(app: dict, cfg: dict) -> str | None:
    street = app.get("street")
    if not street:
        return None
    locality = app.get("locality") or cfg["display_name"].split(",")[0].replace("Village of ", "")
    return f"{street}, {locality}, {cfg['state']}"


def _nominatim_search(query: str, bounded: int, session: requests.Session, bbox: str):
    params = {
        "q": query,
        "format": "json",
        "limit": 3,
        "viewbox": bbox,
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


def geocode(query: str, session: requests.Session, cfg: dict) -> dict | None:
    # Skip obviously bad queries (regex noise)
    lower = query.lower()
    if any(b in lower for b in ("corner of", "entrance to", "along ")):
        return None

    bbox = ",".join(str(v) for v in cfg["bbox"])

    # Try 1: strict viewbox around the municipality
    hits = _nominatim_search(query, bounded=1, session=session, bbox=bbox)
    if hit := _pick(hits):
        return hit

    # Try 2: drop locality, search broader county
    time.sleep(1.1)
    street = query.split(",")[0].strip()
    broad_q = f"{street}, {cfg['county']} County, {cfg['state']}"
    hits = _nominatim_search(broad_q, bounded=0, session=session, bbox=bbox)
    if hit := _pick(hits, require_orange_county=(cfg["county"] == "Orange")):
        return hit

    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", "-m", default=municipality.DEFAULT_SLUG)
    args = ap.parse_args()
    cfg = municipality.load_config(args.slug)
    derived = municipality.derived_dir(args.slug)
    apps_path = derived / "applications.json"
    out_path = derived / "applications_geocoded.json"
    cache_path = municipality.shared_path("geocode_cache.json")

    apps = json.loads(apps_path.read_text())
    cache = load_cache(cache_path)
    session = requests.Session()

    unique_queries = sorted({build_query(a, cfg) for a in apps} - {None})
    print(f"{len(apps)} applications, {len(unique_queries)} unique addresses")

    new_lookups = 0
    for i, q in enumerate(unique_queries):
        if q in cache:
            continue
        try:
            cache[q] = geocode(q, session, cfg)
        except Exception as e:
            print(f"  ERR {q}: {e}")
            cache[q] = None
        new_lookups += 1
        if new_lookups % 10 == 0:
            print(f"  {new_lookups} geocoded... (latest: {q[:60]})")
            save_cache(cache, cache_path)
        time.sleep(1.1)
    save_cache(cache, cache_path)
    print(f"Geocoded {new_lookups} new addresses (cache size: {len(cache)})")

    out = []
    n_mapped = 0
    for a in apps:
        q = build_query(a, cfg)
        geo = cache.get(q) if q else None
        if geo:
            a["lat"] = geo["lat"]
            a["lon"] = geo["lon"]
            a["geo_display_name"] = geo["display_name"]
            n_mapped += 1
        out.append(a)

    out_path.write_text(json.dumps(out, indent=2))
    print(f"Wrote {out_path} — {n_mapped}/{len(apps)} apps mapped "
          f"({100*n_mapped/len(apps):.0f}%)")


if __name__ == "__main__":
    main()
