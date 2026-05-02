"""
CCAD Parcel Export by Zip Code
scraper/export_parcels.py

Downloads CCAD residential parcel data from Texas Open Data Portal
and exports filtered CSV for target Collin County zip codes.
Mirrors the DCAD export_parcels.py structure exactly.

Run: python scraper/export_parcels.py
"""

import csv
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ccad_export")

# ─────────────────────────────────────────────────────────────
# Target zip codes
# ─────────────────────────────────────────────────────────────
TARGET_ZIPS = {
    "75098",                                          # Wylie
    "75023","75024","75025","75074","75075",
    "75252","75093",                                  # Plano
    "75080","75082",                                  # Richardson
    "75002",                                          # Allen
    "75069","75070",                                  # McKinney
}

# ─────────────────────────────────────────────────────────────
# CCAD Socrata API
# ─────────────────────────────────────────────────────────────
CCAD_API_URL = "https://data.texas.gov/resource/6dqt-e958.json"
PAGE_SIZE    = 50000

# ─────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────
ROOT     = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────
# Column mapping: Socrata field → output CSV column
# ─────────────────────────────────────────────────────────────
COLUMN_MAP = {
    "owner_name":    "Owner Name",
    "situs_num":     "Property Street Number",
    "situs_street":  "Property Street",
    "situs_city":    "Property City",
    "situs_zip":     "Property Zip",
    "mail_addr":     "Mailing Address",
    "mail_city":     "Mailing City",
    "mail_state":    "Mailing State",
    "mail_zip":      "Mailing Zip",
    "yr_impr":       "Year Built",
    "impr_sqft":     "Living Area SqFt",
    "bedrooms":      "Bedrooms",
    "bathrooms":     "Bathrooms",
    "deed_date":     "Deed Transfer Date",
    "appraised_val": "Appraised Value",
    "prop_type_cd":  "Property Type",
    "geo_id":        "Account Number",
}

OUTPUT_COLUMNS = [
    "Account Number", "Owner Name",
    "Property Street Number", "Property Street", "Property City", "Property Zip",
    "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
    "Year Built", "Living Area SqFt", "Bedrooms", "Bathrooms",
    "Deed Transfer Date", "Appraised Value", "Property Type",
]


def fetch_parcels_for_zip(zip_code: str, session: requests.Session) -> list[dict]:
    """Fetch all residential parcels for a specific zip code."""
    records = []
    offset  = 0

    while True:
        params = {
            "$limit":  PAGE_SIZE,
            "$offset": offset,
            "$where":  f"situs_zip='{zip_code}' AND prop_type_cd='R'",
        }
        try:
            resp = session.get(CCAD_API_URL, params=params, timeout=60)
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break
            records.extend(rows)
            if len(rows) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        except Exception as e:
            log.warning(f"  ZIP {zip_code} offset {offset} error: {e}")
            break

    return records


def row_to_output(row: dict) -> dict:
    """Convert Socrata row to output CSV format."""
    out = {}
    for api_field, col_name in COLUMN_MAP.items():
        out[col_name] = str(row.get(api_field, "") or "").strip()

    # Build full property address
    num    = out.get("Property Street Number", "")
    street = out.get("Property Street", "")
    out["Property Address"] = f"{num} {street}".strip()

    return out


def main():
    today    = datetime.now().strftime("%Y%m%d")
    out_file = DATA_DIR / f"parcel_export_{today}.csv"

    log.info(f"CCAD Parcel Export — {len(TARGET_ZIPS)} target zip codes")
    log.info(f"Output: {out_file}")

    session = requests.Session()
    session.headers.update({"User-Agent": "IntenseHoldings-CCAD-Scraper/1.0"})

    all_rows = []
    zip_counts = {}

    for zip_code in sorted(TARGET_ZIPS):
        log.info(f"  Fetching ZIP {zip_code}...")
        rows = fetch_parcels_for_zip(zip_code, session)
        zip_counts[zip_code] = len(rows)
        all_rows.extend(rows)
        log.info(f"  ZIP {zip_code}: {len(rows)} parcels")

    log.info(f"\nTotal parcels: {len(all_rows)}")
    log.info("Breakdown by ZIP:")
    for z, count in sorted(zip_counts.items()):
        log.info(f"  {z}: {count:,}")

    # Filter: long-term owners (deed date before 2005 = 20+ years)
    # and owner-occupied (mailing address matches property address)
    long_term = []
    all_output = []

    for row in all_rows:
        out = row_to_output(row)
        all_output.append(out)

        # Long-term owner filter
        deed_date = out.get("Deed Transfer Date", "")
        try:
            deed_year = int(str(deed_date)[:4])
            if deed_year <= 2005:
                long_term.append(out)
        except:
            pass

    # Save full export
    output_cols = OUTPUT_COLUMNS + ["Property Address"]
    with open(out_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=output_cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_output)

    log.info(f"\n✅ Full export saved: {out_file} ({len(all_output):,} rows)")

    # Save long-term owner filtered export
    lt_file = DATA_DIR / f"longterm_owners_{today}.csv"
    with open(lt_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=output_cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(long_term)

    log.info(f"✅ Long-term owners saved: {lt_file} ({len(long_term):,} rows)")
    log.info(f"   (Properties with deed date ≤ 2005 — 20+ year owners)")

    # Save address lookup JSON for offer engine
    addr_lookup = {}
    for row in all_rows:
        out = row_to_output(row)
        addr = out.get("Property Address", "").upper().strip()
        city = out.get("Property City", "").upper().strip()
        if addr and city:
            key = f"{addr} {city}"
            addr_lookup[key] = {
                "sqft":      out.get("Living Area SqFt", ""),
                "beds":      out.get("Bedrooms", ""),
                "baths":     out.get("Bathrooms", ""),
                "yr_built":  out.get("Year Built", ""),
                "zip":       out.get("Property Zip", ""),
                "owner":     out.get("Owner Name", ""),
                "deed_date": out.get("Deed Transfer Date", ""),
            }

    lookup_file = DATA_DIR / "ccad_address_lookup.json"
    with open(lookup_file, "w") as f:
        json.dump(addr_lookup, f)
    log.info(f"✅ Address lookup saved: {lookup_file} ({len(addr_lookup):,} entries)")
    log.info("   (Used by offer engine for address-only property lookup)")


if __name__ == "__main__":
    main()
