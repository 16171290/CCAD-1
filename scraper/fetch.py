"""
Collin County Motivated Seller Lead Scraper
scraper/fetch.py

Pulls LP, NOFC, Probate from Collin County.
Enriches with CCAD parcel data using confirmed column names.
"""

import asyncio, csv, json, logging, os, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("collin_scraper")

LOOKBACK_DAYS      = 7
RESEARCH_TX_COOKIE = os.environ.get("RESEARCH_TX_COOKIE", "")

# Confirmed CCAD API endpoints
CCAD_APIS = [
    ("2025", "https://data.texas.gov/resource/vffy-snc6.json"),
    ("2024", "https://data.texas.gov/resource/6dqt-e958.json"),
]
PAGE_SIZE = 50000

# Correct Collin County Clerk portal URL
CLERK_URL = "https://collin.tx.publicsearch.us/"

TARGET_ZIPS = {
    "75098",
    "75023","75024","75025","75074","75075","75252","75093",
    "75080","75082",
    "75002",
    "75069","75070",
}

ROOT          = Path(__file__).parent.parent
DATA_DIR      = ROOT / "data"
DASHBOARD_DIR = ROOT / "dashboard"
DATA_DIR.mkdir(exist_ok=True)
DASHBOARD_DIR.mkdir(exist_ok=True)

FLAG_DEFS = [
    ("Lis pendens",      lambda r: r["cat"] == "LP"),
    ("Pre-foreclosure",  lambda r: r["cat"] in ("NOFC","TAXDEED")),
    ("Judgment lien",    lambda r: r["cat"] in ("JUD","CCJ")),
    ("Tax lien",         lambda r: r["cat"] in ("LNIRS","LNFED")),
    ("Mechanic lien",    lambda r: r["cat"] == "LNMECH"),
    ("Probate / estate", lambda r: r["cat"] == "PRO"),
    ("LLC / corp owner", lambda r: bool(re.search(
        r"\b(LLC|INC|CORP|LTD|LP|TRUST|HOLDINGS|PROPERTIES|INVESTMENTS)\b",
        r.get("owner",""), re.I))),
    ("New this week",    lambda r: True),
]

def _name_variants(name):
    name = name.upper().strip()
    parts = name.split()
    v = [name]
    if len(parts) >= 2:
        v.append(f"{parts[-1]} {' '.join(parts[:-1])}")
        v.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
    return list(set(v))

def get_field(row, *candidates):
    """Flexible field getter — tries exact, lowercase, underscore variants."""
    for c in candidates:
        for key in [c, c.lower(), c.lower().replace(" ","_"), c.replace(" ","_")]:
            if key in row and row[key] is not None and str(row[key]).strip():
                return str(row[key]).strip()
    return ""

def load_parcel_data():
    """Load CCAD parcel data using confirmed column names from bulk download."""
    log.info("Loading CCAD parcel data...")

    # Find working API
    api_url = None
    for year, url in CCAD_APIS:
        try:
            resp = requests.get(url, params={"$limit": 1}, timeout=20)
            if resp.status_code == 200 and resp.json():
                log.info(f"✅ Using {year} CCAD API: {url}")
                cols = list(resp.json()[0].keys())
                log.info(f"   Columns: {cols[:15]}")
                api_url = url
                break
        except Exception as e:
            log.warning(f"{year} API: {e}")

    if not api_url:
        log.warning("No CCAD API available")
        return {}, {}

    session = requests.Session()
    session.headers["User-Agent"] = "IntenseHoldings/1.0"
    parcel_by_owner   = {}
    parcel_by_address = {}
    total = 0

    for zip_code in TARGET_ZIPS:
        offset = 0
        while True:
            try:
                # Try zip filter — Socrata lowercases "Zip" to "zip"
                params = {
                    "$limit":  PAGE_SIZE,
                    "$offset": offset,
                    "$where":  f"zip='{zip_code}'",
                }
                resp = session.get(api_url, params=params, timeout=60)

                if resp.status_code == 400:
                    # zip field might have different name — fetch without filter
                    params = {"$limit": PAGE_SIZE, "$offset": offset}
                    resp = session.get(api_url, params=params, timeout=60)

                if resp.status_code != 200:
                    break

                rows = resp.json()
                if not rows:
                    break

                for row in rows:
                    # Get zip using confirmed field name variants
                    row_zip = get_field(row, "zip","Zip","situs_zip","prop_zip")
                    if row_zip and row_zip not in TARGET_ZIPS:
                        continue

                    # Use confirmed column names from bulk download
                    owner      = get_field(row,"ownername","ownerName","owner_name").upper()
                    situs_full = get_field(row,"situsconcat","situsConcat","situs_concat",
                                           "situs_address","Situs Address")
                    situs_num  = get_field(row,"situst_street_number","situs_street_number",
                                           "Situst street number")
                    street_nm  = get_field(row,"street_name","Street Name","streetname")
                    city       = get_field(row,"city","City","situs_city")

                    if not situs_full:
                        situs_full = f"{situs_num} {street_nm}".strip()

                    sqft_raw = get_field(row,"imprv_main_area","imprvmainarea",
                                         "imprvMainArea","imprymainarea")
                    sqft = sqft_raw.replace(",","")

                    record = {
                        "prop_address": situs_full,
                        "prop_city":    city,
                        "prop_state":   "TX",
                        "prop_zip":     row_zip,
                        "mail_address": get_field(row,"mailing_address","Mailing address","mail_addr"),
                        "mail_city":    get_field(row,"mailing_city","Mailing City","mail_city"),
                        "mail_state":   get_field(row,"mailing_st","Mailing St","mail_state"),
                        "mail_zip":     get_field(row,"mailing_zip","Mailing zip","mail_zip"),
                        "owner":        owner,
                        "yr_built":     get_field(row,"imprv_year_built","imprvyearbuilt",
                                                   "imprvYearBuilt","yr_built"),
                        "living_area":  sqft,
                        "deed_date":    get_field(row,"deed_date","Deed Date","deeddate"),
                    }

                    if owner:
                        for v in _name_variants(owner):
                            parcel_by_owner[v] = record

                    addr_key = f"{situs_full.upper()} {city.upper()}".strip()
                    if addr_key.strip():
                        parcel_by_address[addr_key] = record

                total += len(rows)
                if len(rows) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE

            except Exception as e:
                log.warning(f"ZIP {zip_code}: {e}")
                break

    log.info(f"CCAD parcels: {total} loaded | "
             f"{len(parcel_by_owner)} owner variants | "
             f"{len(parcel_by_address)} addresses")

    try:
        with open(DATA_DIR / "ccad_address_lookup.json","w") as f:
            json.dump(parcel_by_address, f)
        log.info(f"Address lookup saved: {len(parcel_by_address)} entries")
    except Exception as e:
        log.warning(f"Address lookup save failed: {e}")

    return parcel_by_owner, parcel_by_address

def enrich_record(record, parcel_by_owner):
    owner = record.get("owner","").upper().strip()
    for v in _name_variants(owner) if owner else []:
        p = parcel_by_owner.get(v)
        if p:
            record.update({k: p[k] for k in
                ["prop_address","prop_city","prop_state","prop_zip",
                 "mail_address","mail_city","mail_state","mail_zip"]})
            break
    return record

def score_record(record):
    flags = [lbl for lbl, pred in FLAG_DEFS if pred(record)]
    score = 30 + min(len(flags)*10, 40)
    try:
        amt = float(str(record.get("amount","0")).replace(",","").replace("$","") or 0)
        if amt > 100000: score += 15
        elif amt > 50000: score += 10
    except: pass
    if record.get("prop_address"): score += 5
    return min(score, 100), flags

async def fetch_clerk_records(start_date, end_date):
    """
    Fetch LP/NOFC/Lien records from Collin County Clerk.
    Uses the publicsearch.us REST API that powers the web portal.
    Portal: https://collin.tx.publicsearch.us/
    """
    records = []
    log.info(f"Fetching Collin County Clerk records {start_date} → {end_date}")

    # publicsearch.us API base (Neumo platform used by many TX counties)
    API_BASE   = "https://collin.tx.publicsearch.us"
    SEARCH_API = f"{API_BASE}/api/search/instrument"

    # Doc types to search — Collin County uses these instrument type codes
    DOC_TYPES = [
        ("LP",    "Lis Pendens",            "LP"),
        ("NOFC",  "Notice of Foreclosure",  "NOFC"),
        ("LN",    "Lien",                   "LN"),
        ("JUD",   "Judgment",               "JUD"),
        ("CCJ",   "Certified Judgment",     "CCJ"),
        ("LNIRS", "IRS Lien",               "LNIRS"),
        ("LNFED", "Federal Tax Lien",       "LNFED"),
    ]

    headers = {
        "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":        "application/json, text/plain, */*",
        "Referer":       f"{API_BASE}/",
        "Origin":        API_BASE,
        "Content-Type":  "application/json",
    }

    session = requests.Session()

    # First visit the main page to get session cookies
    try:
        session.get(API_BASE, headers=headers, timeout=15)
    except Exception as e:
        log.warning(f"Clerk session init: {e}")

    for cat, cat_label, doc_code in DOC_TYPES:
        page_num = 0
        while True:
            try:
                # Try REST API endpoint used by the portal
                payload = {
                    "departmentId": "RP",   # Real Property
                    "docTypeCode":  doc_code,
                    "startDate":    start_date,
                    "endDate":      end_date,
                    "page":         page_num,
                    "pageSize":     100,
                    "sortBy":       "recordedDate",
                    "sortOrder":    "desc",
                }
                resp = session.post(SEARCH_API, json=payload,
                                    headers=headers, timeout=30)

                if resp.status_code == 404:
                    # Try alternate endpoint format
                    params = {
                        "type":      "instrument",
                        "docType":   doc_code,
                        "startDate": start_date,
                        "endDate":   end_date,
                        "page":      page_num,
                        "pageSize":  100,
                    }
                    resp = session.get(f"{API_BASE}/api/instruments",
                                       params=params, headers=headers, timeout=30)

                if resp.status_code not in (200, 201):
                    log.info(f"  {cat}: HTTP {resp.status_code} — trying next")
                    break

                data = resp.json()
                hits = (data.get("results") or data.get("hits") or
                        data.get("instruments") or data.get("data") or [])

                if not hits:
                    break

                log.info(f"  {cat}: page {page_num} → {len(hits)} records")

                for item in hits:
                    grantor  = item.get("grantor","") or item.get("grantorName","") or ""
                    grantee  = item.get("grantee","") or item.get("granteeName","") or ""
                    doc_num  = item.get("instrumentNumber","") or item.get("docNumber","") or ""
                    rec_date = item.get("recordedDate","") or item.get("filingDate","") or ""
                    amount   = item.get("amount","") or item.get("consideration","") or ""
                    legal    = item.get("legalDescription","") or item.get("legal","") or ""

                    records.append({
                        "doc_num":      str(doc_num),
                        "doc_type":     cat,
                        "filed":        str(rec_date)[:10],
                        "cat":          cat,
                        "cat_label":    cat_label,
                        "owner":        str(grantor).upper().strip(),
                        "grantee":      str(grantee).upper().strip(),
                        "amount":       str(amount),
                        "legal":        str(legal)[:200],
                        "prop_address": "", "prop_city": "",
                        "prop_state":   "TX", "prop_zip": "",
                        "mail_address": "", "mail_city": "",
                        "mail_state":   "", "mail_zip": "",
                        "clerk_url":    f"{API_BASE}/result/RP/{doc_num}",
                        "flags": [], "score": 30,
                    })

                total_pages = data.get("totalPages", data.get("pages", 1))
                if page_num >= total_pages - 1 or len(hits) < 100:
                    break
                page_num += 1

            except Exception as e:
                log.warning(f"  {cat} page {page_num}: {e}")
                break

    log.info(f"Clerk records total: {len(records)}")
    return records

async def fetch_probate_records(start_date, end_date):
    records = []
    if not RESEARCH_TX_COOKIE:
        log.warning("RESEARCH_TX_COOKIE not set — skipping probate")
        return records

    RESEARCH_TX_BASE = "https://research.txcourts.gov"
    headers = {
        "Cookie": RESEARCH_TX_COOKIE,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Referer": RESEARCH_TX_BASE,
    }

    # Collin County probate court jurisdiction IDs
    jurisdictions = [
        {"id": 422, "name": "Collin County Probate Court"},
        {"id": 423, "name": "Collin County Court at Law 1"},
        {"id": 424, "name": "Collin County Court at Law 2"},
        {"id": 425, "name": "Collin County Court at Law 3"},
        {"id": 426, "name": "Collin County Court at Law 4"},
        {"id": 427, "name": "Collin County Court at Law 5"},
    ]

    for jur in jurisdictions:
        try:
            payload = {
                "jurisdictionId": jur["id"],
                "caseCategory": "Probate",
                "filedDateFrom": start_date,
                "filedDateTo": end_date,
                "page": 0, "pageSize": 99,
            }
            resp = requests.post(
                f"{RESEARCH_TX_BASE}/CaseSearch/api/cases",
                headers=headers, json=payload, timeout=30)
            if resp.status_code == 401:
                log.warning("re:SearchTX cookie expired")
                print("cookie expired")
                return records
            if resp.status_code != 200:
                continue
            hits = resp.json().get("hits", resp.json().get("cases", []))
            for case in hits:
                desc = case.get("description","") or ""
                records.append({
                    "doc_num":   case.get("caseNumber",""),
                    "doc_type":  "PRO",
                    "filed":     (case.get("dateFiled","") or "")[:10],
                    "cat":       "PRO", "cat_label": "Probate",
                    "owner":     _extract_probate_name(desc),
                    "grantee":   "", "amount": "", "legal": desc,
                    "prop_address":"","prop_city":"","prop_state":"TX","prop_zip":"",
                    "mail_address":"","mail_city":"","mail_state":"","mail_zip":"",
                    "clerk_url": f"{RESEARCH_TX_BASE}/CaseDetail/{case.get('caseDataID','')}",
                    "flags":[], "score":30,
                })
        except Exception as e:
            log.warning(f"Probate {jur['name']}: {e}")

    log.info(f"Probate records: {len(records)}")
    return records

def _extract_probate_name(desc):
    for pat in [
        r"IN RE[:\s]+THE ESTATE OF ([A-Z][A-Z\s,\.]+?),?\s*DECEASED",
        r"ESTATE OF ([A-Z][A-Z\s,\.]+?),?\s*DECEASED",
    ]:
        m = re.search(pat, desc.upper())
        if m:
            name = re.sub(r"\s+", " ", m.group(1).strip().rstrip(","))
            if 3 < len(name) < 60:
                return name
    return desc[:80]

def save_outputs(records, date_range):
    now = datetime.now(timezone.utc).isoformat()
    with_addr = sum(1 for r in records if r.get("prop_address"))
    payload = {
        "fetched_at": now, "source": "Collin County",
        "date_range": date_range,
        "total": len(records), "with_address": with_addr,
        "records": records,
    }
    for d in [DATA_DIR, DASHBOARD_DIR]:
        with open(d/"records.json","w") as f:
            json.dump(payload, f, indent=2, default=str)

    today    = datetime.now().strftime("%Y%m%d")
    ghl_file = DATA_DIR / f"ghl_export_{today}.csv"
    fields   = [
        "First Name","Last Name","Mailing Address","Mailing City",
        "Mailing State","Mailing Zip","Property Address","Property City",
        "Property State","Property Zip","Lead Type","Document Type",
        "Date Filed","Document Number","Amount/Debt Owed",
        "Seller Score","Motivated Seller Flags","Source","Public Records URL",
    ]
    with open(ghl_file,"w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in records:
            parts = r.get("owner","").split()
            w.writerow({
                "First Name":           parts[0].title() if parts else "",
                "Last Name":            " ".join(parts[1:]).title() if len(parts)>1 else "",
                "Mailing Address":      r.get("mail_address",""),
                "Mailing City":         r.get("mail_city",""),
                "Mailing State":        r.get("mail_state",""),
                "Mailing Zip":          r.get("mail_zip",""),
                "Property Address":     r.get("prop_address",""),
                "Property City":        r.get("prop_city",""),
                "Property State":       r.get("prop_state","TX"),
                "Property Zip":         r.get("prop_zip",""),
                "Lead Type":            r.get("cat_label",""),
                "Document Type":        r.get("doc_type",""),
                "Date Filed":           r.get("filed",""),
                "Document Number":      r.get("doc_num",""),
                "Amount/Debt Owed":     r.get("amount",""),
                "Seller Score":         r.get("score",0),
                "Motivated Seller Flags": ", ".join(r.get("flags",[])),
                "Source":               "Collin County",
                "Public Records URL":   r.get("clerk_url",""),
            })
    log.info(f"GHL CSV: {ghl_file} ({len(records)} rows)")
    log.info(f"━━━ Complete: {len(records)} records ({with_addr} with address)")

async def main():
    end_dt    = datetime.now(timezone.utc)
    start_dt  = end_dt - timedelta(days=LOOKBACK_DAYS)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str   = end_dt.strftime("%Y-%m-%d")
    log.info(f"Collin County scraper: {start_str} → {end_str}")

    parcel_by_owner, _ = load_parcel_data()
    clerk   = await fetch_clerk_records(start_str, end_str)
    probate = await fetch_probate_records(start_str, end_str)
    all_rec = clerk + probate

    final = []
    for rec in all_rec:
        try:
            rec = enrich_record(rec, parcel_by_owner)
            rec["score"], rec["flags"] = score_record(rec)
            final.append(rec)
        except Exception as e:
            log.warning(f"Enrich failed {rec.get('doc_num','?')}: {e}")

    save_outputs(final, {"start": start_str, "end": end_str})

if __name__ == "__main__":
    asyncio.run(main())
