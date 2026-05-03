"""
Collin County Motivated Seller Lead Scraper
scraper/fetch.py

Uses confirmed CCAD field names from official Field Descriptions document.
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
CLERK_URL          = "https://collin.tx.publicsearch.us/"

# Confirmed Socrata API endpoints
CCAD_APIS = [
    ("2025", "https://data.texas.gov/resource/vffy-snc6.json"),
    ("2024", "https://data.texas.gov/resource/6dqt-e958.json"),
]
PAGE_SIZE = 50000

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

def gf(row, *keys):
    """Get first non-empty value trying multiple key variants."""
    for k in keys:
        for variant in [k, k.lower(), k.upper()]:
            val = row.get(variant)
            if val is not None and str(val).strip():
                return str(val).strip()
    return ""


# ─────────────────────────────────────────────────────────────
# PARCEL DATA
# Confirmed field names from CCAD Field Descriptions document
# ─────────────────────────────────────────────────────────────

def load_parcel_data():
    """
    Load CCAD parcel data using official field names:
      file_as_name  = Owner Name
      situs_num     = Street Number
      situs_street  = Street Name
      situs_city    = Property City
      situs_zip     = Property Zip
      situs_display = Full 1-line address
      addr_line1    = Mailing Address
      addr_city     = Mailing City
      addr_state    = Mailing State
      addr_zip      = Mailing Zip
      living_area   = SqFt
      yr_blt        = Year Built
      deed_dt       = Deed Date
      prop_type_cd  = R=Residential
      beds          = Bedrooms
      baths         = Bathrooms
    """
    log.info("Loading CCAD parcel data (confirmed field names)...")

    api_url = None
    for year, url in CCAD_APIS:
        try:
            resp = requests.get(url, params={"$limit": 1}, timeout=20)
            if resp.status_code == 200 and resp.json():
                cols = list(resp.json()[0].keys())
                log.info(f"✅ {year} API active")
                log.info(f"   Columns: {cols}")
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

    for zip_code in sorted(TARGET_ZIPS):
        offset   = 0
        zip_rows = []
        log.info(f"  ZIP {zip_code}...")

        while True:
            try:
                # Use confirmed field name: situs_zip
                params = {
                    "$limit":  PAGE_SIZE,
                    "$offset": offset,
                    "$where":  f"situs_zip='{zip_code}' AND prop_type_cd='R'",
                }
                resp = session.get(api_url, params=params, timeout=90)

                if resp.status_code == 400:
                    # Fallback without prop_type filter
                    params["$where"] = f"situs_zip='{zip_code}'"
                    resp = session.get(api_url, params=params, timeout=90)

                if resp.status_code == 400:
                    # Last resort — try zip field name variants
                    for zip_field in ["zip","situs_zip","addr_zip"]:
                        params["$where"] = f"{zip_field}='{zip_code}'"
                        resp = session.get(api_url, params=params, timeout=90)
                        if resp.status_code == 200:
                            break

                if resp.status_code != 200:
                    log.warning(f"  ZIP {zip_code}: HTTP {resp.status_code}")
                    break

                rows = resp.json()
                if not rows:
                    break

                for row in rows:
                    # Confirmed field names from CCAD Field Descriptions
                    owner       = gf(row,"file_as_name").upper()
                    situs_disp  = gf(row,"situs_display")
                    situs_num   = gf(row,"situs_num")
                    situs_st    = gf(row,"situs_street")
                    situs_city  = gf(row,"situs_city")
                    situs_zip   = gf(row,"situs_zip") or zip_code
                    mail_line1  = gf(row,"addr_line1")
                    mail_city   = gf(row,"addr_city")
                    mail_state  = gf(row,"addr_state")
                    mail_zip    = gf(row,"addr_zip")
                    living_area = gf(row,"living_area").replace(",","")
                    yr_blt      = gf(row,"yr_blt")
                    deed_dt     = gf(row,"deed_dt")
                    beds        = gf(row,"beds")
                    baths       = gf(row,"baths")

                    # Build full address from situs_display or components
                    if not situs_disp:
                        parts = [p for p in [situs_num, situs_st] if p]
                        situs_disp = " ".join(parts)

                    record = {
                        "prop_address": situs_disp,
                        "prop_city":    situs_city,
                        "prop_state":   "TX",
                        "prop_zip":     situs_zip,
                        "mail_address": mail_line1,
                        "mail_city":    mail_city,
                        "mail_state":   mail_state,
                        "mail_zip":     mail_zip,
                        "owner":        owner,
                        "yr_built":     yr_blt,
                        "living_area":  living_area,
                        "beds":         beds,
                        "baths":        baths,
                        "deed_date":    deed_dt,
                    }

                    # Index by owner name
                    if owner:
                        for v in _name_variants(owner):
                            parcel_by_owner[v] = record

                    # Index by property address
                    addr_key = f"{situs_disp.upper()} {situs_city.upper()}".strip()
                    if addr_key.strip():
                        parcel_by_address[addr_key] = record

                zip_rows.extend(rows)
                total += len(rows)
                if len(rows) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE

            except Exception as e:
                log.warning(f"  ZIP {zip_code}: {e}")
                break

        log.info(f"  ZIP {zip_code}: {len(zip_rows):,} parcels")

    log.info(f"CCAD total: {total:,} parcels | "
             f"{len(parcel_by_owner):,} owner variants | "
             f"{len(parcel_by_address):,} addresses")

    try:
        with open(DATA_DIR/"ccad_address_lookup.json","w") as f:
            json.dump(parcel_by_address, f)
        log.info(f"Address lookup saved: {len(parcel_by_address):,} entries")
    except Exception as e:
        log.warning(f"Address lookup save: {e}")

    return parcel_by_owner, parcel_by_address


# ─────────────────────────────────────────────────────────────
# CLERK RECORDS — Playwright scraper
# ─────────────────────────────────────────────────────────────

async def fetch_clerk_records(start_date, end_date):
    """
    Scrape LP/NOFC/Lien from Collin County Clerk portal.
    Intercepts API calls made by the React portal.
    """
    records = []
    log.info(f"Fetching Collin County Clerk records {start_date} → {end_date}")

    DOC_TYPES = [
        ("LP",    "Lis Pendens"),
        ("NOFC",  "Notice of Foreclosure"),
        ("LN",    "Lien"),
        ("JUD",   "Judgment"),
        ("LNIRS", "IRS Lien"),
        ("LNFED", "Federal Tax Lien"),
    ]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width":1280,"height":800},
        )
        page = await ctx.new_page()
        intercepted = []

        async def on_response(response):
            try:
                url = response.url
                if response.status == 200 and any(
                    x in url for x in ["search","instrument","result","api"]
                ):
                    ct = response.headers.get("content-type","")
                    if "json" in ct:
                        data = await response.json()
                        intercepted.append({"url":url,"data":data})
                        log.info(f"  Intercepted: {url[:80]}")
            except: pass

        page.on("response", on_response)

        try:
            await page.goto(CLERK_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            log.info(f"Clerk portal loaded")

            for doc_type, doc_label in DOC_TYPES:
                intercepted.clear()
                log.info(f"  Searching: {doc_type}")
                try:
                    # Find search input
                    inp = await page.query_selector(
                        "input[type='text']:visible, input[type='search']:visible")
                    if inp:
                        await inp.triple_click()
                        await inp.type(doc_type, delay=80)
                        await page.wait_for_timeout(300)

                    # Click search
                    btn = await page.query_selector(
                        "button[type='submit']:visible, "
                        "button:has-text('Search'):visible")
                    if btn:
                        await btn.click()
                        await page.wait_for_load_state("networkidle", timeout=10000)
                        await page.wait_for_timeout(2000)

                    # Parse intercepted API responses
                    for item_data in intercepted:
                        data = item_data["data"]
                        items = (data.get("results") or data.get("hits") or
                                 data.get("instruments") or data.get("data") or
                                 (data if isinstance(data, list) else []))
                        for item in (items if isinstance(items, list) else []):
                            if not isinstance(item, dict):
                                continue
                            grantor  = (item.get("grantor") or item.get("grantorName") or
                                       item.get("name") or "")
                            doc_num  = (item.get("instrumentNumber") or
                                       item.get("docNumber") or item.get("id") or "")
                            rec_date = (item.get("recordedDate") or
                                       item.get("filingDate") or item.get("date") or "")
                            # Filter by date range
                            if rec_date:
                                rec_d = str(rec_date)[:10]
                                if rec_d < start_date or rec_d > end_date:
                                    continue
                            records.append({
                                "doc_num":      str(doc_num),
                                "doc_type":     doc_type,
                                "filed":        str(rec_date)[:10],
                                "cat":          doc_type,
                                "cat_label":    doc_label,
                                "owner":        str(grantor).upper().strip(),
                                "grantee":      str(item.get("grantee","") or "").upper(),
                                "amount":       str(item.get("amount","") or ""),
                                "legal":        str(item.get("legalDescription","") or "")[:200],
                                "prop_address": "","prop_city":"",
                                "prop_state":   "TX","prop_zip":"",
                                "mail_address": "","mail_city":"",
                                "mail_state":   "","mail_zip":"",
                                "clerk_url":    f"{CLERK_URL}result/RP/{doc_num}",
                                "flags":[],"score":30,
                            })

                    # Reset for next search
                    await page.goto(CLERK_URL, timeout=15000)
                    await page.wait_for_load_state("networkidle", timeout=10000)

                except Exception as e:
                    log.warning(f"  {doc_type}: {e}")
                    try: await page.goto(CLERK_URL, timeout=10000)
                    except: pass

        except Exception as e:
            log.warning(f"Clerk error: {e}")
        finally:
            await browser.close()

    log.info(f"Clerk records: {len(records)}")
    return records


# ─────────────────────────────────────────────────────────────
# PROBATE — re:SearchTX
# ─────────────────────────────────────────────────────────────

async def fetch_probate_records(start_date, end_date):
    records = []
    if not RESEARCH_TX_COOKIE:
        log.warning("RESEARCH_TX_COOKIE not set — skipping probate")
        return records

    RESEARCH_TX_BASE = "https://research.txcourts.gov"
    headers = {
        "Cookie":       RESEARCH_TX_COOKIE,
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "User-Agent":   "Mozilla/5.0",
        "Referer":      RESEARCH_TX_BASE,
    }
    jurisdictions = [
        {"id": 422, "name": "Collin Probate Court"},
        {"id": 423, "name": "Collin CCL 1"},
        {"id": 424, "name": "Collin CCL 2"},
        {"id": 425, "name": "Collin CCL 3"},
        {"id": 426, "name": "Collin CCL 4"},
        {"id": 427, "name": "Collin CCL 5"},
    ]
    for jur in jurisdictions:
        try:
            resp = requests.post(
                f"{RESEARCH_TX_BASE}/CaseSearch/api/cases",
                headers=headers, timeout=30,
                json={"jurisdictionId":jur["id"],"caseCategory":"Probate",
                      "filedDateFrom":start_date,"filedDateTo":end_date,
                      "page":0,"pageSize":99})
            if resp.status_code == 401:
                log.warning("re:SearchTX cookie expired")
                print("cookie expired")
                return records
            if resp.status_code != 200: continue
            for case in resp.json().get("hits", resp.json().get("cases",[])):
                desc = case.get("description","") or ""
                records.append({
                    "doc_num":   case.get("caseNumber",""),
                    "doc_type":  "PRO","filed":(case.get("dateFiled","") or "")[:10],
                    "cat":       "PRO","cat_label":"Probate",
                    "owner":     _extract_probate_name(desc),
                    "grantee":   "","amount":"","legal":desc,
                    "prop_address":"","prop_city":"","prop_state":"TX","prop_zip":"",
                    "mail_address":"","mail_city":"","mail_state":"","mail_zip":"",
                    "clerk_url":f"{RESEARCH_TX_BASE}/CaseDetail/{case.get('caseDataID','')}",
                    "flags":[],"score":30,
                })
        except Exception as e:
            log.warning(f"Probate {jur['name']}: {e}")

    log.info(f"Probate: {len(records)}")
    return records

def _extract_probate_name(desc):
    for pat in [r"IN RE[:\s]+THE ESTATE OF ([A-Z][A-Z\s,\.]+?),?\s*DECEASED",
                r"ESTATE OF ([A-Z][A-Z\s,\.]+?),?\s*DECEASED"]:
        m = re.search(pat, desc.upper())
        if m:
            name = re.sub(r"\s+"," ",m.group(1).strip().rstrip(","))
            if 3 < len(name) < 60: return name
    return desc[:80]


# ─────────────────────────────────────────────────────────────
# ENRICH, SCORE, SAVE
# ─────────────────────────────────────────────────────────────

def enrich_record(record, parcel_by_owner):
    owner = record.get("owner","").upper().strip()
    for v in (_name_variants(owner) if owner else []):
        p = parcel_by_owner.get(v)
        if p:
            record.update({k:p[k] for k in
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

def save_outputs(records, date_range):
    now = datetime.now(timezone.utc).isoformat()
    with_addr = sum(1 for r in records if r.get("prop_address"))
    payload = {"fetched_at":now,"source":"Collin County","date_range":date_range,
               "total":len(records),"with_address":with_addr,"records":records}
    for d in [DATA_DIR, DASHBOARD_DIR]:
        with open(d/"records.json","w") as f:
            json.dump(payload, f, indent=2, default=str)

    today    = datetime.now().strftime("%Y%m%d")
    ghl_file = DATA_DIR / f"ghl_export_{today}.csv"
    fields   = ["First Name","Last Name","Mailing Address","Mailing City",
                 "Mailing State","Mailing Zip","Property Address","Property City",
                 "Property State","Property Zip","Lead Type","Document Type",
                 "Date Filed","Document Number","Amount/Debt Owed",
                 "Seller Score","Motivated Seller Flags","Source","Public Records URL"]
    with open(ghl_file,"w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in records:
            parts = r.get("owner","").split()
            w.writerow({
                "First Name":    parts[0].title() if parts else "",
                "Last Name":     " ".join(parts[1:]).title() if len(parts)>1 else "",
                "Mailing Address":    r.get("mail_address",""),
                "Mailing City":       r.get("mail_city",""),
                "Mailing State":      r.get("mail_state",""),
                "Mailing Zip":        r.get("mail_zip",""),
                "Property Address":   r.get("prop_address",""),
                "Property City":      r.get("prop_city",""),
                "Property State":     r.get("prop_state","TX"),
                "Property Zip":       r.get("prop_zip",""),
                "Lead Type":          r.get("cat_label",""),
                "Document Type":      r.get("doc_type",""),
                "Date Filed":         r.get("filed",""),
                "Document Number":    r.get("doc_num",""),
                "Amount/Debt Owed":   r.get("amount",""),
                "Seller Score":       r.get("score",0),
                "Motivated Seller Flags": ", ".join(r.get("flags",[])),
                "Source":             "Collin County",
                "Public Records URL": r.get("clerk_url",""),
            })
    log.info(f"GHL CSV: {ghl_file} ({len(records)} rows)")
    log.info(f"━━━ Complete: {len(records)} records ({with_addr} with address)")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

async def main():
    end_dt    = datetime.now(timezone.utc)
    start_dt  = end_dt - timedelta(days=LOOKBACK_DAYS)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str   = end_dt.strftime("%Y-%m-%d")
    log.info(f"Collin County scraper: {start_str} → {end_str}")

    parcel_by_owner, _ = load_parcel_data()
    clerk   = await fetch_clerk_records(start_str, end_str)
    probate = await fetch_probate_records(start_str, end_str)

    final = []
    for rec in clerk + probate:
        try:
            rec = enrich_record(rec, parcel_by_owner)
            rec["score"], rec["flags"] = score_record(rec)
            final.append(rec)
        except Exception as e:
            log.warning(f"Enrich: {e}")

    save_outputs(final, {"start":start_str,"end":end_str})

if __name__ == "__main__":
    asyncio.run(main())
