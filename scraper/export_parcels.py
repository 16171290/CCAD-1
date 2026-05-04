"""
CCAD Parcel Export
scraper/export_parcels.py

Confirmed Socrata API field names (discovered 2026-05-04):
situszip, situscity, situsconcat, situsbldgnum, situsstreetname
ownername, owneraddrline1, owneraddrcity, owneraddrstate, owneraddrzip
imprvmainarea, imprvyearbuilt, deedeffdate, proptype, geoid
currvalmarket, currvalappraised, prevvalmarket, prevvalappraised
"""

import csv, json, logging, sys
from datetime import datetime
from pathlib import Path
import requests

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("ccad_export")

TARGET_ZIPS = [
    "75002",
    "75023","75024","75025","75074","75075","75252","75093",
    "75080","75082",
    "75069","75070",
    "75098",
]

CCAD_API  = "https://data.texas.gov/resource/vffy-snc6.json"
PAGE_SIZE = 50000

ROOT     = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

OUTPUT_COLUMNS = [
    "Account Number","Owner Name",
    "Property Address","Property City","Property Zip",
    "Mailing Address","Mailing City","Mailing State","Mailing Zip",
    "Year Built","Living Area SqFt","Deed Date",
    "Market Value","Appraised Value","Property Type","Long Term Owner",
]

def gv(row, *keys):
    for k in keys:
        val = row.get(k)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""

def fetch_zip(session, zip_code):
    """Fetch all parcels for one zip code. No shared state."""
    results = []
    page    = 0

    while True:
        query_params = {
            "$limit":  PAGE_SIZE,
            "$offset": page * PAGE_SIZE,
            "$where":  f"situszip='{zip_code}'",
        }

        try:
            r = session.get(CCAD_API, params=query_params, timeout=300)

            if r.status_code == 400:
                log.warning(f"  ZIP {zip_code} filter error: {r.text[:80]}")
                # Fallback: fetch without filter
                fallback_params = {
                    "$limit":  PAGE_SIZE,
                    "$offset": page * PAGE_SIZE,
                }
                r = session.get(CCAD_API, params=fallback_params, timeout=300)

            if r.status_code != 200:
                log.warning(f"  ZIP {zip_code}: HTTP {r.status_code}")
                break

            batch = r.json()
            if not batch:
                break

            # Log sample on first page
            if page == 0 and batch:
                s = batch[0]
                log.info(f"  ZIP {zip_code} sample → "
                         f"situszip={s.get('situszip','?')} | "
                         f"proptype={s.get('proptype','?')} | "
                         f"ownername={str(s.get('ownername','?'))[:25]}")

            for row in batch:
                # Local zip filter (for fallback fetches)
                rz = str(row.get("situszip","") or "").strip()
                if rz and rz != zip_code:
                    continue

                # Build address
                addr = gv(row,"situsconcat","situsconcatshort")
                if not addr:
                    num = gv(row,"situsbldgnum")
                    st  = gv(row,"situsstreetname")
                    addr = f"{num} {st}".strip()

                sqft = gv(row,"imprvmainarea").replace(",","")

                results.append({
                    "Account Number":   gv(row,"geoid","propid"),
                    "Owner Name":       gv(row,"ownername"),
                    "Property Address": addr,
                    "Property City":    gv(row,"situscity"),
                    "Property Zip":     rz or zip_code,
                    "Mailing Address":  gv(row,"owneraddrline1"),
                    "Mailing City":     gv(row,"owneraddrcity"),
                    "Mailing State":    gv(row,"owneraddrstate"),
                    "Mailing Zip":      gv(row,"owneraddrzip"),
                    "Year Built":       gv(row,"imprvyearbuilt"),
                    "Living Area SqFt": sqft,
                    "Deed Date":        gv(row,"deedeffdate"),
                    "Market Value":     gv(row,"currvalmarket","prevvalmarket"),
                    "Appraised Value":  gv(row,"currvalappraised","prevvalappraised"),
                    "Property Type":    gv(row,"proptype"),
                    "Long Term Owner":  "",
                })

            if len(batch) < PAGE_SIZE:
                break
            page += 1

        except Exception as e:
            log.warning(f"  ZIP {zip_code} page {page}: {e}")
            break

    return results


def main():
    today = datetime.now().strftime("%Y%m%d")
    log.info(f"CCAD Parcel Export — {len(TARGET_ZIPS)} target zip codes")

    session = requests.Session()
    session.headers["User-Agent"] = "IntenseHoldings/1.0"

    all_rows   = []
    long_term  = []
    addr_lookup= {}
    zip_counts = {}

    for zip_code in TARGET_ZIPS:
        log.info(f"  ZIP {zip_code}...")
        rows = fetch_zip(session, zip_code)
        zip_counts[zip_code] = len(rows)
        log.info(f"  ZIP {zip_code}: {len(rows):,} parcels")

        for out in rows:
            # Long-term owner: deed year ≤ 2005
            deed = out.get("Deed Date","")
            try:
                is_lt = int(str(deed)[:4]) <= 2005
            except:
                is_lt = False
            out["Long Term Owner"] = "YES" if is_lt else "NO"

            all_rows.append(out)
            if is_lt:
                long_term.append(out)

            # Address lookup for offer engine
            addr = out.get("Property Address","").upper().strip()
            city = out.get("Property City","").upper().strip()
            if addr:
                entry = {
                    "sqft":      out.get("Living Area SqFt",""),
                    "yr_built":  out.get("Year Built",""),
                    "zip":       out.get("Property Zip",""),
                    "owner":     out.get("Owner Name",""),
                    "deed_date": out.get("Deed Date",""),
                }
                addr_lookup[f"{addr} {city}".strip()] = entry
                addr_lookup[addr] = entry

    # Save full export
    out_file = DATA_DIR / f"parcel_export_{today}.csv"
    with open(out_file,"w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)
    log.info(f"✅ Full export: {out_file} ({len(all_rows):,} rows)")

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
    for z in TARGET_ZIPS:
        log.info(f"  {z}: {zip_counts.get(z,0):,}")
    log.info(f"Long-term owners: {len(long_term):,}")

if __name__ == "__main__":
    main()
