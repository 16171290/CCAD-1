"""
CCAD Parcel Export by Zip Code
scraper/export_parcels.py

Uses confirmed field names from CCAD Field Descriptions document (4/11/2013):
  file_as_name  = Property Owner Name
  addr_line1    = Mailing Address Line 1
  addr_city     = Mailing Address City
  addr_state    = Mailing Address State
  addr_zip      = Mailing Address Zip Code
  situs_num     = Property Address Bldg/House Number
  situs_street  = Property Address Street Name
  situs_city    = Property Address City
  situs_zip     = Property Address Zip Code
  situs_display = Property Address Display (1 line address)
  living_area   = Improvement Main Area SqFt Total
  yr_blt        = Improvement/Building Actual Year Built
  deed_dt       = Deed Effective Date (most recent)
  prop_type_cd  = R=Residential
  beds          = Number of Bedrooms
  baths         = Number of Bathrooms
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

CCAD_APIS = [
    ("2025", "https://data.texas.gov/resource/vffy-snc6.json"),
    ("2024", "https://data.texas.gov/resource/6dqt-e958.json"),
]
PAGE_SIZE = 50000

ROOT     = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

OUTPUT_COLUMNS = [
    "Account Number","Owner Name",
    "Property Address","Property City","Property Zip",
    "Mailing Address","Mailing City","Mailing State","Mailing Zip",
    "Year Built","Living Area SqFt","Beds","Baths",
    "Deed Date","Market Value","Appraised Value",
    "Property Type","Long Term Owner",
]

def find_api():
    for year, url in CCAD_APIS:
        try:
            resp = requests.get(url, params={"$limit":1}, timeout=20)
            if resp.status_code == 200 and resp.json():
                log.info(f"✅ {year} API: {url}")
                return url
        except Exception as e:
            log.warning(f"{year}: {e}")
    return None

def gf(row, *keys):
    for k in keys:
        for v in [k, k.lower(), k.upper()]:
            val = row.get(v)
            if val is not None and str(val).strip():
                return str(val).strip()
    return ""

def map_row(row):
    """Map using confirmed CCAD field names."""
    # Confirmed Socrata API field names from discovery:
    situs_disp = gf(row,"situsconcat","situsconcatshort")
    situs_num  = gf(row,"situsbldgnum")
    situs_st   = gf(row,"situsstreetname")
    if not situs_disp:
        situs_disp = f"{situs_num} {situs_st}".strip()

    sqft = gf(row,"imprvmainarea").replace(",","")

    return {
        "Account Number":   gf(row,"geoid","propid"),
        "Owner Name":       gf(row,"ownername"),
        "Property Address": situs_disp,
        "Property City":    gf(row,"situscity"),
        "Property Zip":     gf(row,"situszip"),
        "Mailing Address":  gf(row,"owneraddrline1"),
        "Mailing City":     gf(row,"owneraddrcity"),
        "Mailing State":    gf(row,"owneraddrstate"),
        "Mailing Zip":      gf(row,"owneraddrzip"),
        "Year Built":       gf(row,"imprvyearbuilt"),
        "Living Area SqFt": sqft,
        "Beds":             gf(row,"beds"),
        "Baths":            gf(row,"baths"),
        "Deed Date":        gf(row,"deedeffdate"),
        "Market Value":     gf(row,"currvalmarket","prevvalmarket"),
        "Appraised Value":  gf(row,"currvalappraised","prevvalappraised"),
        "Property Type":    gf(row,"proptype"),
        "Long Term Owner":  "",
    }

def main():
    today    = datetime.now().strftime("%Y%m%d")
    out_file = DATA_DIR / f"parcel_export_{today}.csv"

    log.info(f"CCAD Parcel Export — {len(TARGET_ZIPS)} target zip codes")

    api_url = find_api()
    if not api_url:
        log.error("No API available")
        return

    session = requests.Session()
    session.headers["User-Agent"] = "IntenseHoldings/1.0"

    all_rows   = []
    zip_counts = {}

    for zip_code in sorted(TARGET_ZIPS):
        log.info(f"  ZIP {zip_code}...")
        offset   = 0
        zip_rows = []

        while True:
            try:
                # Confirmed field names: situszip, proptype
                params["$where"] = f"situszip='{zip_code}' AND proptype='R'"
                resp = session.get(api_url, params=params, timeout=300)

                if resp.status_code == 400:
                    params = {"$limit": PAGE_SIZE, "$offset": offset}
                    resp = session.get(api_url, params=params, timeout=300)

                if resp.status_code != 200:
                    log.warning(f"  ZIP {zip_code}: HTTP {resp.status_code}")
                    break

                rows = resp.json()
                if not rows:
                    break

                # Local filter fallback if $where not applied
                rows = [r for r in rows
                        if str(r.get("situszip","") or "").strip() == zip_code
                        and str(r.get("proptype","R") or "R").strip() in ("R","")]

                zip_rows.extend(rows)
                if len(rows) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE

            except Exception as e:
                log.warning(f"  ZIP {zip_code}: {e}")
                break

        zip_counts[zip_code] = len(zip_rows)
        all_rows.extend(zip_rows)
        log.info(f"  ZIP {zip_code}: {len(zip_rows):,} parcels")

    log.info(f"Total: {len(all_rows):,} parcels")

    residential = []
    long_term   = []
    addr_lookup = {}

    for row in all_rows:
        out = map_row(row)

        # Long-term owner: deed date ≤ 2005
        deed = out.get("Deed Date","")
        try:
            is_lt = int(str(deed)[:4]) <= 2005
        except:
            is_lt = False
        out["Long Term Owner"] = "YES" if is_lt else "NO"

        residential.append(out)
        if is_lt:
            long_term.append(out)

        # Address lookup for offer engine
        addr = out.get("Property Address","").upper().strip()
        city = out.get("Property City","").upper().strip()
        # Also index by short address (without city)
        if addr and city:
            for key in [f"{addr} {city}", addr]:
                addr_lookup[key] = {
                    "sqft":      out.get("Living Area SqFt",""),
                    "beds":      out.get("Beds",""),
                    "baths":     out.get("Baths",""),
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
    log.info(f"✅ Long-term owners: {lt_file} ({len(long_term):,} rows)")

    # Save address lookup
    with open(DATA_DIR/"ccad_address_lookup.json","w") as f:
        json.dump(addr_lookup, f)
    log.info(f"✅ Address lookup: {len(addr_lookup):,} entries")

    log.info("\nBreakdown by ZIP:")
    for z, cnt in sorted(zip_counts.items()):
        log.info(f"  {z}: {cnt:,}")
    log.info(f"Long-term owners (deed ≤ 2005): {len(long_term):,}")

if __name__ == "__main__":
    main()
