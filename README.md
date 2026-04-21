# ZBA — Village of Woodbury Zoning Applications Viewer

Plots Village of Woodbury Zoning Board of Appeals applications on a map, with a year-range slider for filtering across time.

The pipeline extracts structured records from ZBA meeting-minutes PDFs, geocodes street addresses via OpenStreetMap's Nominatim, and renders them in a static Leaflet page.

## Quick start

```bash
# 1. Install deps (also requires system tesseract for OCR: `brew install tesseract`)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Put ZBA meeting-minutes PDFs in pdfs/Zoning_Board/<year>/*.pdf
#    (available from the Village of Woodbury website)

# 3. Run the pipeline
.venv/bin/python ocr.py            # OCRs scanned PDFs in place (idempotent)
.venv/bin/python extract_zba.py    # → applications.json
.venv/bin/python geocode.py        # → applications_geocoded.json

# 4. Serve the viewer (with chat)
export ANTHROPIC_API_KEY=sk-ant-...   # for the chat feature
.venv/bin/python chat_server.py
# open http://localhost:8765

# Without a Claude API key the map works but /chat returns an error.
# Fallback without chat: python3 -m http.server 8765
```

## Files

| File | Purpose |
|---|---|
| `ocr.py` | Add a text layer to scanned/image-only PDFs with `ocrmypdf --skip-text`. Idempotent; runs in place. Needed for 2014–2018 minutes which were scanned rather than born-digital. |
| `extract_zba.py` | Parse ZBA PDFs, extract application records (date, label, name, address, tax map, zoning district, request type). Falls back to folder-path year when OCR mangles the in-text date. Outputs `applications.json`. |
| `geocode.py` | Geocode each unique address via Nominatim. Caches results. Outputs `applications_geocoded.json`. |
| `index.html` | Static Leaflet viewer with year-range slider, per-board toggles, and chat panel. Loads `applications_geocoded.json`. No build step. |
| `chat_server.py` | Serves the static viewer and a `/chat` endpoint. Retrieves relevant applications by keyword/entity/address match, calls Claude Opus 4.7 with the retrieved context, returns the answer + citations. Requires `ANTHROPIC_API_KEY`. |
| `applications_geocoded.json` | Shipped data snapshot so the viewer works without re-running the pipeline. |

## Known limitations

- **~52% of extracted records map to coordinates.** Rest either lack a street address in the minutes (decision-review sections, concatenated parcels) or reference tax maps only. Tax-map-to-parcel geocoding would require Orange County GIS data.
- **Applicant name is the section label, often a last name.** LLC / full-name resolution is future work.

## Data source

All ZBA meeting minutes are public records published by the Village of Woodbury, NY. The PDFs themselves are not committed to this repo; point `extract_zba.py` at a local copy.
