# ZBA — Village of Woodbury Zoning Applications Viewer

Plots Village of Woodbury Zoning Board of Appeals applications on a map, with a year-range slider for filtering across time.

The pipeline extracts structured records from ZBA meeting-minutes PDFs, geocodes street addresses via OpenStreetMap's Nominatim, and renders them in a static Leaflet page.

## Quick start

```bash
# 1. Install deps
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Put ZBA meeting-minutes PDFs in pdfs/Zoning_Board/<year>/*.pdf
#    (available from the Village of Woodbury website)

# 3. Run the pipeline
.venv/bin/python extract_zba.py    # → applications.json
.venv/bin/python geocode.py        # → applications_geocoded.json

# 4. Serve the viewer
python3 -m http.server 8765
# open http://localhost:8765
```

## Files

| File | Purpose |
|---|---|
| `extract_zba.py` | Parse ZBA PDFs, extract application records (date, label, name, address, tax map, zoning district, request type). Outputs `applications.json`. |
| `geocode.py` | Geocode each unique address via Nominatim. Caches results. Outputs `applications_geocoded.json`. |
| `index.html` | Static Leaflet viewer with year-range slider. Loads `applications_geocoded.json`. No build step. |
| `applications_geocoded.json` | Shipped data snapshot so the viewer works without re-running the pipeline. |

## Known limitations

- **OCR needed for 2014–2016 and 2018 minutes** — those PDFs are scanned images that `pdfplumber` can't extract. Gaps show in the timeline.
- **~45% of extracted records map to coordinates.** Rest either lack a street address in the minutes (decision-review sections, concatenated parcels) or reference tax maps only. Tax-map-to-parcel geocoding would require Orange County GIS data.
- **Applicant name is the section label, often a last name.** LLC / full-name resolution is future work.

## Data source

All ZBA meeting minutes are public records published by the Village of Woodbury, NY. The PDFs themselves are not committed to this repo; point `extract_zba.py` at a local copy.
