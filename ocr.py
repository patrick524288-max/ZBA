"""Run ocrmypdf over every scanned ZBA PDF in place.

A PDF is considered scanned if pdfplumber extracts 0 characters total.
ocrmypdf --skip-text is idempotent and atomic (temp file + rename), so
reruns are safe even if a previous run was interrupted.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import pdfplumber

import municipality


def is_scanned(path: Path) -> bool:
    try:
        with pdfplumber.open(path) as pdf:
            return sum(len(p.extract_text() or "") for p in pdf.pages) == 0
    except Exception:
        return False


def ocr(path: Path) -> tuple[bool, str]:
    # Write output to same path; ocrmypdf uses temp+rename so it's atomic.
    r = subprocess.run(
        [".venv/bin/ocrmypdf", "--skip-text", "--quiet",
         str(path), str(path)],
        capture_output=True, text=True,
    )
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", "-m", default=municipality.DEFAULT_SLUG)
    args = ap.parse_args()
    cfg = municipality.load_config(args.slug)
    pdf_root = municipality.pdfs_dir(args.slug)
    boards = list(cfg["boards"].values())  # folder names

    scanned = []
    for board in boards:
        root = pdf_root / board
        if not root.exists():
            continue
        board_scanned = [p for p in sorted(root.rglob("*.pdf")) if is_scanned(p)]
        print(f"  {board}: {len(board_scanned)} scanned", flush=True)
        scanned.extend(board_scanned)
    print(f"{len(scanned)} scanned PDFs to OCR total", flush=True)
    t0 = time.time()
    ok = fail = 0
    for i, p in enumerate(scanned, 1):
        success, msg = ocr(p)
        if success:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL {p}: {msg[:200]}", flush=True)
        elapsed = time.time() - t0
        if i % 5 == 0 or i == len(scanned):
            print(f"  [{i}/{len(scanned)}] ok={ok} fail={fail} "
                  f"elapsed={elapsed:.0f}s avg={elapsed/i:.1f}s/file",
                  flush=True)
    print(f"Done. OCR'd {ok} files in {time.time()-t0:.0f}s "
          f"({fail} failures)")


if __name__ == "__main__":
    sys.exit(main() or 0)
