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
    "Account Number","First Name","Last Name",
    "Mailing Address","Mailing City","Mailing State","Mailing Zip",
    "Property Address","Property City","Property Zip",
    "Deed Year","Year Built","Living Area SqFt",
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
                # Local zip filter
                rz = str(row.get("situszip","") or "").strip()
                if rz and rz != zip_code:
                    continue

                # Keep Real property only — exclude Personal, Mineral etc
                pt = str(row.get("proptype","") or "").strip().lower()
                if pt and pt not in ("real","r","residential"):
                    continue

                # Exclude large commercial buildings — SFR typically under 6,000 sqft
                sqft_raw = str(row.get("imprvmainarea","") or "").replace(",","").strip()
                try:
                    if sqft_raw and float(sqft_raw) > 6000:
                        continue
                except: pass

                # Exclude entities — companies, HOAs, cities, trusts etc.
                owner_raw = str(row.get("ownername","") or "").upper().strip()
                entity_keywords = [
                    "LLC","INC","CORP","LTD"," LP ","L.P.","L.L.C.",
                    "TRUST","TRUSTEE","ESTATE OF",
                    "HOA","HOMEOWNERS","HOMEOWNER ASSOC",
                    "CITY OF","COUNTY OF","STATE OF",
                    "ASSOCIATION","ASSOC ","ASSN ",
                    "PROPERTIES","HOLDINGS","INVESTMENTS","VENTURES",
                    "REALTY","DEVELOPMENT","DEVELOPERS",
                    "PARTNERS","PARTNERSHIP"," GROUP","FUND ",
                    "CHURCH","SCHOOL DISTRICT","AUTHORITY",
                    "BANK","FINANCIAL","MORTGAGE","CAPITAL",
                    "CROSSING","LIMITED",
                ]
                if any(kw in owner_raw for kw in entity_keywords):
                    continue

                # Build clean address — use situsconcat, strip ", CITY, TX ZIP" suffix
                full = gv(row,"situsconcat","situsconcatshort")
                addr = full.split(",")[0].strip() if full else ""
                if not addr:
                    num  = gv(row,"situsbldgnum").strip()
                    st   = gv(row,"situsstreetname").strip()
                    addr = f"{num} {st}".strip()

                # Exclude apartments/condos
                apt_check = re.compile(
                    r"\b(APT|UNIT|STE|SUITE|BLDG|FL|FLOOR)\b|\d+/\d+",
                    re.IGNORECASE
                )
                # Also detect trailing unit numbers like "3801 14TH ST 1901"
                trailing_unit = re.compile(r"\s+\d{3,4}[A-Z]?\s*$")
                if apt_check.search(addr) or trailing_unit.search(addr):
                    continue

                # Use situsconcat and strip ", CITY, TX ZIP" suffix (kept for fallback ref)
                # e.g. "5703 ABINGDON DR , RICHARDSON, TX 75082" → "5703 ABINGDON DR"
                full = gv(row,"situsconcat","situsconcatshort")
                addr = full.split(",")[0].strip() if full else ""
                # Fallback to components
                if not addr:
                    num  = gv(row,"situsbldgnum").strip()
                    st   = gv(row,"situsstreetname").strip()
                    addr = f"{num} {st}".strip()

                sqft = gv(row,"imprvmainarea").replace(",","")

                # Split owner name into First / Last
                # CCAD format: "LASTNAME FIRSTNAME" or "LASTNAME FIRSTNAME &" for joint
                owner_full = gv(row,"ownername").strip()

                if "&" in owner_full:
                    # Joint owners e.g. "STAUFFER JANE &" or "YANCEY BRENT &"
                    # Remove trailing & and split
                    clean = owner_full.replace("&","").strip()
                    parts = clean.split()
                    if len(parts) >= 2:
                        last_name  = parts[0].title()
                        first_name = " ".join(parts[1:]).title() + " &"
                    else:
                        last_name  = clean.title()
                        first_name = ""
                elif len(owner_full.split()) >= 2:
                    # Standard: LASTNAME FIRSTNAME MIDDLE
                    parts      = owner_full.split()
                    last_name  = parts[0].title()
                    first_name = " ".join(parts[1:]).title()
                elif owner_full:
                    # Single name
                    last_name  = owner_full.title()
                    first_name = ""
                else:
                    last_name  = ""
                    first_name = ""

                # Year only from deed date
                deed_full = gv(row,"deedeffdate")
                deed_year = str(deed_full)[:4] if deed_full else ""

                results.append({
                    "Account Number":   gv(row,"geoid","propid"),
                    "First Name":       first_name,
                    "Last Name":        last_name,
                    "Mailing Address":  gv(row,"owneraddrline1"),
                    "Mailing City":     gv(row,"owneraddrcity"),
                    "Mailing State":    gv(row,"owneraddrstate"),
                    "Mailing Zip":      gv(row,"owneraddrzip"),
                    "Property Address": addr,
                    "Property City":    gv(row,"situscity"),
                    "Property Zip":     rz or zip_code,
                    "Deed Year":        deed_year,
                    "Year Built":       gv(row,"imprvyearbuilt"),
                    "Living Area SqFt": sqft,
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
            deed = out.get("Deed Year","")
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
                    "owner":     f"{out.get('First Name','')} {out.get('Last Name','')}".strip(),
                    "deed_date": out.get("Deed Year",""),
                }
                addr_lookup[f"{addr} {city}".strip()] = entry
                addr_lookup[addr] = entry

    def sort_key(x):
        city = x.get("Property City","").upper().strip()
        addr = x.get("Property Address","").upper().strip()
        parts = addr.split(" ", 1)
        try:
            street_num  = int(parts[0])
            street_name = parts[1].strip() if len(parts) > 1 else ""
        except ValueError:
            street_num  = 0
            street_name = addr
        # Sort: Street Name → Street Number → City
        return (street_name, street_num, city)

    all_rows.sort(key=sort_key)
    long_term.sort(key=sort_key)

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
