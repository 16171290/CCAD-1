"""
CCAD Parcel Export by Zip Code
scraper/export_parcels.py

Uses confirmed CCAD column names from Texas Open Data Portal.
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

# Confirmed CCAD column names from dataset
# propYear, propID, geoID, propType, propSubType,
# "Situst street number", situsStreetPrefix, "Street Name",
# situsStreetSuffix, "Situs Address", City, Zip, situsConcat,
# ownerName, ownerName2, "Mailing address", ownerAddrLine2,
# "Mailing City", "Mailing St", "Mailing zip", ownerAddrCountry,
# deedTypeCd, deedNum, deedBook, deedPage, "Deed Date", deedFileDate,
# imprvYearBuilt, imprvClassCd, imprvMainArea, imprvUnits,
# imprvPoolFlag, imprvCategoryCodes, landTypeCode, landSizeAcres,
# landSizeSqft, landAgAcres, landCategoryCodes, exemptCodes,
# prevVal* fields

# Socrata API — try 2025 first then 2024
CCAD_APIS = [
    "https://data.texas.gov/resource/vffy-snc6.json",  # 2025
    "https://data.texas.gov/resource/6dqt-e958.json",  # 2024
]
PAGE_SIZE = 50000

ROOT     = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# Map confirmed column names → output names
# Note: Socrata lowercases and replaces spaces with underscores
SOCRATA_FIELD_MAP = {
    # Socrata API field      : output column name
    "geoid":                  "Account Number",
    "proptype":               "Property Type",
    "ownername":              "Owner Name",
    "ownername2":             "Owner Name 2",
    "situst_street_number":   "Property Street Number",
    "situsstreetprefix":      "Street Prefix",
    "street_name":            "Street Name",
    "situsstreetsuffix":      "Street Suffix",
    "situs_address":          "Situs Address",
    "city":                   "Property City",
    "zip":                    "Property Zip",
    "situsconcat":            "Full Situs Address",
    "mailing_address":        "Mailing Address",
    "owneraddrline2":         "Mailing Address 2",
    "mailing_city":           "Mailing City",
    "mailing_st":             "Mailing State",
    "mailing_zip":            "Mailing Zip",
    "deed_date":              "Deed Date",
    "deedfiledate":           "Deed File Date",
    "imprvyearbuilt":         "Year Built",
    "imprvclasskcd":          "Improvement Class",
    "imprymainarea":          "Living Area SqFt",
    "imprvunits":             "Units",
    "prevvalmarket":          "Market Value",
    "prevvalappraised":       "Appraised Value",
}

OUTPUT_COLUMNS = [
    "Account Number", "Property Type", "Owner Name", "Owner Name 2",
    "Full Situs Address", "Property Street Number", "Street Name",
    "Property City", "Property Zip",
    "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
    "Year Built", "Living Area SqFt", "Deed Date", "Deed File Date",
    "Market Value", "Appraised Value",
]


def find_working_api():
    """Find which API endpoint and field names work."""
    for url in CCAD_APIS:
        try:
            resp = requests.get(url, params={"$limit": 2}, timeout=30)
            if resp.status_code == 200:
                rows = resp.json()
                if rows:
                    log.info(f"✅ API working: {url}")
                    log.info(f"   Columns: {list(rows[0].keys())[:15]}")
                    return url, list(rows[0].keys())
        except Exception as e:
            log.warning(f"API {url}: {e}")
    return None, []


def map_row(row, columns):
    """Map a Socrata row to output using flexible field matching."""
    col_lower = {c.lower().replace(" ","_").replace("-","_"): c for c in columns}
    
    def get(*candidates):
        for c in candidates:
            key = c.lower().replace(" ","_").replace("-","_")
            if key in col_lower:
                return str(row.get(col_lower[key], "") or "").strip()
        return ""

    situs_num  = get("situst_street_number", "situs_street_number", "str_num", "house_num")
    street_pfx = get("situsstreetprefix", "street_prefix", "str_pfx")
    street_nm  = get("street_name", "str_name", "situs_street")
    street_sfx = get("situsstreetsuffix", "street_suffix", "str_sfx")
    situs_full = get("situsconcat", "situs_address", "situs_addr")

    # Build full address if situsConcat not available
    if not situs_full:
        parts = [p for p in [situs_num, street_pfx, street_nm, street_sfx] if p]
        situs_full = " ".join(parts)

    return {
        "Account Number":        get("geoid","geo_id","propid","prop_id"),
        "Property Type":         get("proptype","prop_type","propsubtype"),
        "Owner Name":            get("ownername","owner_name","owner"),
        "Owner Name 2":          get("ownername2","owner_name2"),
        "Full Situs Address":    situs_full,
        "Property Street Number":situs_num,
        "Street Name":           street_nm,
        "Property City":         get("city","situs_city","prop_city"),
        "Property Zip":          get("zip","situs_zip","prop_zip","zipcode"),
        "Mailing Address":       get("mailing_address","mail_addr","owneraddrline1"),
        "Mailing City":          get("mailing_city","mail_city"),
        "Mailing State":         get("mailing_st","mail_state","mailing_state"),
        "Mailing Zip":           get("mailing_zip","mail_zip"),
        "Year Built":            get("imprvyearbuilt","imprv_year_built","yr_built","year_built"),
        "Living Area SqFt":      get("imprymainarea","imprvmainarea","imprv_main_area","living_sqft","sqft"),
        "Deed Date":             get("deed_date","deeddate"),
        "Deed File Date":        get("deedfiledate","deed_file_date"),
        "Market Value":          get("prevvalmarket","prev_val_market","market_value"),
        "Appraised Value":       get("prevvalappraised","prev_val_appraised","appraised_val"),
    }


def main():
    today    = datetime.now().strftime("%Y%m%d")
    out_file = DATA_DIR / f"parcel_export_{today}.csv"

    log.info(f"CCAD Parcel Export — {len(TARGET_ZIPS)} target zip codes")

    api_url, columns = find_working_api()
    if not api_url:
        log.error("No CCAD API available — cannot export parcels")
        return

    session = requests.Session()
    session.headers["User-Agent"] = "IntenseHoldings-CCAD/1.0"

    all_rows   = []
    zip_counts = {}

    for zip_code in sorted(TARGET_ZIPS):
        log.info(f"  Fetching ZIP {zip_code}...")
        offset = 0
        zip_rows = []

        # Find zip field name
        zip_field = None
        for candidate in ["zip","situs_zip","prop_zip","zipcode","zip_code"]:
            if any(c.lower().replace(" ","_") == candidate for c in columns):
                zip_field = candidate
                break

        while True:
            try:
                if zip_field:
                    params = {
                        "$limit":  PAGE_SIZE,
                        "$offset": offset,
                        "$where":  f"{zip_field}='{zip_code}'",
                    }
                else:
                    params = {"$limit": PAGE_SIZE, "$offset": offset}

                resp = session.get(api_url, params=params, timeout=60)
                if resp.status_code != 200:
                    log.warning(f"  ZIP {zip_code}: HTTP {resp.status_code}")
                    break

                rows = resp.json()
                if not rows:
                    break

                # Filter locally if no zip filter applied
                if not zip_field:
                    rows = [r for r in rows
                            if str(r.get("zip","") or r.get("situs_zip","") or "").strip() == zip_code]

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

    log.info(f"\nTotal: {len(all_rows):,} parcels")

    # Convert and filter residential only
    residential = []
    long_term   = []
    addr_lookup = {}

    for row in all_rows:
        out = map_row(row, columns)

        # Filter residential (propType A=residential in CCAD)
        prop_type = out.get("Property Type","").upper()
        if prop_type and prop_type not in ("A","R","RESIDENTIAL","") :
            if not any(x in prop_type for x in ["A","R",""]):
                continue

        residential.append(out)

        # Long-term owner filter (deed date ≤ 2005)
        deed = out.get("Deed Date","")
        try:
            yr = int(str(deed)[:4])
            if yr <= 2005:
                long_term.append(out)
        except: pass

        # Build address lookup for offer engine
        addr  = out.get("Full Situs Address","").upper().strip()
        city  = out.get("Property City","").upper().strip()
        if addr and city:
            addr_lookup[f"{addr} {city}"] = {
                "sqft":      out.get("Living Area SqFt",""),
                "yr_built":  out.get("Year Built",""),
                "zip":       out.get("Property Zip",""),
                "owner":     out.get("Owner Name",""),
                "deed_date": out.get("Deed Date",""),
            }

    # Save full export
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
    log.info(f"✅ Long-term owners: {lt_file} ({len(long_term):,} rows, deed ≤ 2005)")

    # Save address lookup
    lookup_file = DATA_DIR / "ccad_address_lookup.json"
    with open(lookup_file,"w") as f:
        json.dump(addr_lookup, f)
    log.info(f"✅ Address lookup: {lookup_file} ({len(addr_lookup):,} entries)")

    log.info("\nBreakdown by ZIP:")
    for z, cnt in sorted(zip_counts.items()):
        log.info(f"  {z}: {cnt:,}")

if __name__ == "__main__":
    main()
