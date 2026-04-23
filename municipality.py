"""Helpers for locating per-municipality files and loading config.

All scripts and the server import from here so paths live in exactly one
place. The only thing outside this file that should know a slug is the
URL path on the frontend.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).parent
MUNICIPALITIES = ROOT / "municipalities"
SHARED = ROOT / "shared"

DEFAULT_SLUG = "woodbury-ny"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def validate_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug):
        raise ValueError(f"Invalid municipality slug: {slug!r}")
    return slug


def municipality_dir(slug: str) -> Path:
    return MUNICIPALITIES / validate_slug(slug)


@lru_cache(maxsize=64)
def load_config(slug: str) -> dict:
    path = municipality_dir(slug) / "config.json"
    return json.loads(path.read_text())


def pdfs_dir(slug: str) -> Path:
    return municipality_dir(slug) / "pdfs"


def derived_dir(slug: str) -> Path:
    d = municipality_dir(slug) / "derived"
    d.mkdir(parents=True, exist_ok=True)
    return d


def assets_dir(slug: str) -> Path:
    d = municipality_dir(slug) / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def assets_src_dir(slug: str) -> Path:
    d = municipality_dir(slug) / "assets_src"
    d.mkdir(parents=True, exist_ok=True)
    return d


def shared_path(name: str) -> Path:
    SHARED.mkdir(exist_ok=True)
    return SHARED / name


def list_slugs() -> list[str]:
    if not MUNICIPALITIES.exists():
        return []
    return sorted(
        p.name for p in MUNICIPALITIES.iterdir()
        if p.is_dir() and (p / "config.json").is_file()
    )
