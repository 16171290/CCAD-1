"""
CCAD Parcel Export by Zip Code
scraper/export_parcels.py

Downloads CCAD bulk data via Socrata API using confirmed column names
from the Texas Open Data Portal bulk download.

Confirmed columns (tab-separated bulk file):
propYear, propID, geoID, propType, propSubType,
"Situst street number", situsStreetPrefix, "Street Name",
situsStreetSuffix, "Situs Address", City, Zip, situsConcat,
ownerName, ownerName2, "Mailing address", ownerAddrLine2,
"Mailing City", "Mailing St", "Mailing zip", ownerAddrCountry,
deedTypeCd, deedNum, deedBook, deedPage, "Deed Date", deedFileDate,
imprvYearBuilt, imprvClassCd, imprvMainArea, imprvUnits,
imprvPoolFlag, imprvCategoryCodes, landTypeCode, landSizeAcres,
landSizeSqft, landAgAcres, landCategoryCodes, exemptCodes,
prevValYear, prevValImprv, prevValLand, prevValMarket,
prevValAgLoss, prevValAppraised, prevValHSCapLoss,
prevValNHSCapLoss, prevValAssessed
"""

import csv, json, logging, sys
from datetime import datetime
from pathlib import Path
import requests

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("ccad_export")

TARGET_ZIPS = {
    "75098",
    "75023","75024","75025","75074","75075","75252","75093",
    "75080","75082",
    "75002",
    "75069","75070",
}

# Socrata API — column names are lowercased with spaces→underscores
# Confirmed mappings from bulk file column names:
CCAD_APIS = [
    ("2025", "https://data.texas.gov/resource/vffy-snc6.json"),
    ("2024", "https://data.texas.gov/resource/6dqt-e958.json"),
]
PAGE_SIZE = 50000

ROOT     = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

OUTPUT_COLUMNS = [
    "geoID", "propType", "propSubType",
    "ownerName", "ownerName2",
    "situsConcat", "siusStreetNumber", "streetName", "City", "Zip",
    "mailingAddress", "mailingCity", "mailingSt", "mailingZip",
    "imprvYearBuilt", "imprvMainArea",
    "deedDate", "deedFileDate",
    "prevValMarket", "prevValAppraised",
    "longTermOwner",
]


def find_api():
    """Find working API and discover actual Socrata column names."""
    for year, url in CCAD_APIS:
        try:
            resp = requests.get(url, params={"$limit": 2}, timeout=30)
            if resp.status_code == 200:
                rows = resp.json()
                if rows:
                    cols = list(rows[0].keys())
                    log.info(f"✅ {year} API working: {url}")
                    log.info(f"   Sample columns: {cols[:20]}")
                    return url, cols
        except Exception as e:
            log.warning(f"{year} API error: {e}")
    return None, []


def get_field(row, *candidates):
    """Get field value trying multiple possible column name variants."""
    for c in candidates:
        # Try exact
        if c in row and row[c] is not None:
            return str(row[c]).strip()
        # Try lowercase
        cl = c.lower()
        if cl in row and row[cl] is not None:
            return str(row[cl]).strip()
        # Try with underscores replacing spaces
        cu = c.lower().replace(" ","_")
        if cu in row and row[cu] is not None:
            return str(row[cu]).strip()
    return ""


def map_row(row):
    """
    Map a Socrata API row to standard output using confirmed field names.
    Socrata lowercases headers and replaces spaces with underscores.
    """
    # Confirmed field mappings:
    # Bulk file col          → Socrata API key (lowercase + underscores)
    # "Situst street number" → situst_street_number
    # "Street Name"          → street_name
    # "Situs Address"        → situs_address
    # City                   → city
    # Zip                    → zip
    # situsConcat            → situsconcat
    # ownerName              → ownername
    # ownerName2             → ownername2
    # "Mailing address"      → mailing_address
    # "Mailing City"         → mailing_city
    # "Mailing St"           → mailing_st
    # "Mailing zip"          → mailing_zip
    # imprvYearBuilt         → imprv_year_built or imprvyearbuilt
    # imprvMainArea          → imprv_main_area or imprymainarea
    # "Deed Date"            → deed_date
    # deedFileDate           → deedfiledate
    # prevValMarket          → prev_val_market or prevvalmarket
    # prevValAppraised       → prev_val_appraised or prevvalappraised

    situs_full = get_field(row,
        "situsconcat","situsConcat","situs_concat",
        "situs_address","Situs Address")

    situs_num = get_field(row,
        "situst_street_number","situs_street_number",
        "Situst street number","street_number")

    street_name = get_field(row,
        "street_name","Street Name","streetname","situs_street")

    sqft_raw = get_field(row,
        "imprv_main_area","imprvmainarea","imprymainarea",
        "imprvMainArea","living_area","sqft")
    # Remove commas from numbers like "1,795"
    sqft = sqft_raw.replace(",","") if sqft_raw else ""

    deed_date = get_field(row,
        "deed_date","Deed Date","deeddate","deed_dt")

    return {
        "geoID":          get_field(row,"geoid","geoID","geo_id","propid"),
        "propType":       get_field(row,"proptype","propType","prop_type"),
        "propSubType":    get_field(row,"propsubtype","propSubType","prop_sub_type"),
        "ownerName":      get_field(row,"ownername","ownerName","owner_name","owner"),
        "ownerName2":     get_field(row,"ownername2","ownerName2","owner_name2"),
        "situsConcat":    situs_full,
        "siusStreetNumber": situs_num,
        "streetName":     street_name,
        "City":           get_field(row,"city","City","situs_city","prop_city"),
        "Zip":            get_field(row,"zip","Zip","situs_zip","prop_zip","zipcode"),
        "mailingAddress": get_field(row,"mailing_address","Mailing address","mail_addr"),
        "mailingCity":    get_field(row,"mailing_city","Mailing City","mail_city"),
        "mailingSt":      get_field(row,"mailing_st","Mailing St","mail_state"),
        "mailingZip":     get_field(row,"mailing_zip","Mailing zip","mail_zip"),
        "imprvYearBuilt": get_field(row,"imprv_year_built","imprvyearbuilt",
                                     "imprvYearBuilt","yr_built","year_built"),
        "imprvMainArea":  sqft,
        "deedDate":       deed_date,
        "deedFileDate":   get_field(row,"deedfiledate","deedFileDate","deed_file_date"),
        "prevValMarket":  get_field(row,"prev_val_market","prevvalmarket",
                                     "prevValMarket","market_value"),
        "prevValAppraised": get_field(row,"prev_val_appraised","prevvalappraised",
                                       "prevValAppraised","appraised_val"),
        "longTermOwner":  "",  # filled in below
    }


def main():
    today    = datetime.now().strftime("%Y%m%d")
    out_file = DATA_DIR / f"parcel_export_{today}.csv"

    log.info(f"CCAD Parcel Export — {len(TARGET_ZIPS)} target zip codes")

    api_url, columns = find_api()
    if not api_url:
        log.error("No CCAD API accessible")
        return

    session = requests.Session()
    session.headers["User-Agent"] = "IntenseHoldings-CCAD/1.0"

    all_rows   = []
    zip_counts = {}

    # Determine zip field name in this API
    zip_candidates = ["zip","situs_zip","prop_zip","zipcode","zip_code"]
    zip_field = next((c for c in zip_candidates
                      if any(col.lower() == c for col in columns)), None)
    log.info(f"Zip field detected: {zip_field}")

    for zip_code in sorted(TARGET_ZIPS):
        log.info(f"  Fetching ZIP {zip_code}...")
        offset    = 0
        zip_rows  = []

        while True:
            try:
                params = {"$limit": PAGE_SIZE, "$offset": offset}
                if zip_field:
                    params["$where"] = f"{zip_field}='{zip_code}'"

                resp = session.get(api_url, params=params, timeout=90)
                if resp.status_code != 200:
                    log.warning(f"  ZIP {zip_code}: HTTP {resp.status_code} — {resp.text[:100]}")
                    break

                rows = resp.json()
                if not rows:
                    break

                # Filter locally by zip if API filter wasn't available
                if not zip_field:
                    rows = [r for r in rows
                            if str(r.get("zip","") or r.get("situs_zip","")
                                   or r.get("Zip","") or "").strip() == zip_code]

                # Filter residential only: propSubType = "Residential"
                rows = [r for r in rows
                        if str(r.get("propsubtype","") or r.get("propSubType","")
                               or r.get("prop_sub_type","") or "Residential").strip()
                        in ("Residential","")]

                zip_rows.extend(rows)
                if len(rows) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE

            except Exception as e:
                log.warning(f"  ZIP {zip_code} error: {e}")
                break

        zip_counts[zip_code] = len(zip_rows)
        all_rows.extend(zip_rows)
        log.info(f"  ZIP {zip_code}: {len(zip_rows):,} parcels")

    log.info(f"\nTotal parcels fetched: {len(all_rows):,}")

    # Convert rows and build outputs
    residential = []
    long_term   = []
    addr_lookup = {}

    for row in all_rows:
        out = map_row(row)

        # Long-term owner: deed date ≤ 2005 (20+ years)
        deed = out.get("deedDate","")
        try:
            yr = int(str(deed)[:4])
            is_lt = yr <= 2005
        except:
            is_lt = False
        out["longTermOwner"] = "YES" if is_lt else "NO"

        residential.append(out)
        if is_lt:
            long_term.append(out)

        # Address lookup for offer engine
        addr = out.get("situsConcat","").upper().strip()
        city = out.get("City","").upper().strip()
        if not addr:
            num  = out.get("siusStreetNumber","")
            st   = out.get("streetName","")
            addr = f"{num} {st}".strip().upper()
        if addr and city:
            addr_lookup[f"{addr} {city}"] = {
                "sqft":      out.get("imprvMainArea",""),
                "yr_built":  out.get("imprvYearBuilt",""),
                "zip":       out.get("Zip",""),
                "owner":     out.get("ownerName",""),
                "deed_date": out.get("deedDate",""),
            }

    # Save full parcel export
    with open(out_file,"w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(residential)
    log.info(f"✅ Full export: {out_file} ({len(residential):,} rows)")

    # Save long-term owners
    lt_file = DATA_DIR / f"longterm_owners_{today}.csv"
    with open(lt_file,"w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(long_term)
    log.info(f"✅ Long-term owners: {lt_file} ({len(long_term):,} rows — deed ≤ 2005)")

    # Save address lookup JSON for offer engine
    lookup_file = DATA_DIR / "ccad_address_lookup.json"
    with open(lookup_file,"w") as f:
        json.dump(addr_lookup, f)
    log.info(f"✅ Address lookup: {lookup_file} ({len(addr_lookup):,} entries)")

    # Summary
    log.info("\nBreakdown by ZIP:")
    for z, cnt in sorted(zip_counts.items()):
        log.info(f"  {z}: {cnt:,}")
    log.info(f"\nLong-term owners (deed ≤ 2005): {len(long_term):,}")
    log.info(f"Address lookup entries: {len(addr_lookup):,}")


if __name__ == "__main__":
    main()
