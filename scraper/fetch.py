"""
Collin County Motivated Seller Lead Scraper
scraper/fetch.py

Mirrors the Dallas County (DCAD) scraper structure exactly.
Pulls LP, NOFC, Probate from Collin County Clerk portal.
Enriches with CCAD parcel data from Texas Open Data Portal.
Scores leads 0-100 and writes JSON + CSV outputs.

Run     : python scraper/fetch.py
Schedule: 07:00 UTC daily via GitHub Actions
"""

import asyncio
import csv
import io
import json
import logging
import os
import re
import sys
import time
import traceback
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("collin_scraper")

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────
LOOKBACK_DAYS = 7

# Collin County Clerk records search
CLERK_SEARCH_URL = "https://countyclerk.collincountytx.gov/online-services/official-public-records"

# re:SearchTX for probate (same platform as Dallas)
RESEARCH_TX_BASE    = "https://research.txcourts.gov"
RESEARCH_TX_SEARCH  = f"{RESEARCH_TX_BASE}/CaseSearch"
RESEARCH_TX_COOKIE  = os.environ.get("RESEARCH_TX_COOKIE", "")

# CCAD parcel data — Texas Open Data Portal API
# Dataset ID: 6dqt-e958 (2024 Certified with monthly updates)
CCAD_SOCRATA_URL = "https://data.texas.gov/resource/6dqt-e958.json"
CCAD_SOCRATA_LIMIT = 50000

# Target Collin County zip codes
TARGET_ZIPS = {
    "75098",  # Wylie
    "75023", "75024", "75025", "75074", "75075", "75252", "75093",  # Plano
    "75080", "75082",  # Richardson
    "75002",  # Allen
    "75069", "75070",  # McKinney
}

# Doc type map
DOC_TYPE_MAP = {
    "LP":       ("LP",    "Lis Pendens"),
    "RELLP":    ("RELLP", "Release Lis Pendens"),
    "NOFC":     ("NOFC",  "Notice of Foreclosure"),
    "TAXDEED":  ("TAXDEED","Tax Deed"),
    "JUD":      ("JUD",   "Judgment"),
    "CCJ":      ("CCJ",   "Certified Judgment"),
    "LNIRS":    ("LNIRS", "IRS Lien"),
    "LNFED":    ("LNFED", "Federal Lien"),
    "LN":       ("LN",    "Lien"),
    "LNMECH":   ("LNMECH","Mechanic Lien"),
    "LNHOA":    ("LNHOA", "HOA Lien"),
    "PRO":      ("PRO",   "Probate"),
}

FLAG_DEFS = [
    ("Lis pendens",      lambda r: r["cat"] == "LP"),
    ("Pre-foreclosure",  lambda r: r["cat"] in ("NOFC", "TAXDEED")),
    ("Judgment lien",    lambda r: r["cat"] in ("JUD", "CCJ")),
    ("Tax lien",         lambda r: r["cat"] in ("LNIRS", "LNFED")),
    ("Mechanic lien",    lambda r: r["cat"] == "LNMECH"),
    ("Probate / estate", lambda r: r["cat"] == "PRO"),
    ("LLC / corp owner", lambda r: bool(re.search(
        r"\b(LLC|INC|CORP|LTD|LP|TRUST|HOLDINGS|PROPERTIES|INVESTMENTS)\b",
        r.get("owner", ""), re.I))),
    ("New this week",    lambda r: True),
]

# ─────────────────────────────────────────────────────────────
# Output paths
# ─────────────────────────────────────────────────────────────
ROOT          = Path(__file__).parent.parent
DATA_DIR      = ROOT / "data"
DASHBOARD_DIR = ROOT / "dashboard"
DATA_DIR.mkdir(exist_ok=True)
DASHBOARD_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────
# CCAD Parcel Data (Texas Open Data Portal / Socrata API)
# ─────────────────────────────────────────────────────────────

def _name_variants(full_name: str) -> list[str]:
    """Generate name lookup variants: FIRST LAST, LAST FIRST, LAST, FIRST"""
    name = full_name.upper().strip()
    parts = name.split()
    variants = [name]
    if len(parts) >= 2:
        variants.append(f"{parts[-1]} {' '.join(parts[:-1])}")
        variants.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
    return list(set(variants))


def load_parcel_data() -> dict[str, dict]:
    """
    Load CCAD parcel data from Texas Open Data Portal (Socrata API).
    Returns dict keyed by owner name variants for fast lookup.
    Also builds address-keyed dict for direct address lookup.
    """
    log.info("Loading CCAD parcel data from Texas Open Data Portal...")
    parcel_by_owner:   dict[str, dict] = {}
    parcel_by_address: dict[str, dict] = {}

    try:
        offset = 0
        total_loaded = 0
        session = requests.Session()

        while True:
            params = {
                "$limit":  CCAD_SOCRATA_LIMIT,
                "$offset": offset,
                "$where":  "prop_type_cd='R'",  # Residential only
            }
            resp = session.get(CCAD_SOCRATA_URL, params=params, timeout=60)
            resp.raise_for_status()
            rows = resp.json()

            if not rows:
                break

            for row in rows:
                # Extract key fields — CCAD Socrata column names
                owner      = str(row.get("owner_name", "") or "").upper().strip()
                situs_num  = str(row.get("situs_num",  "") or "").strip()
                situs_st   = str(row.get("situs_street","") or "").strip()
                situs_city = str(row.get("situs_city", "") or "").strip()
                situs_zip  = str(row.get("situs_zip",  "") or "").strip()
                mail_addr  = str(row.get("mail_addr",  "") or "").strip()
                mail_city  = str(row.get("mail_city",  "") or "").strip()
                mail_state = str(row.get("mail_state", "") or "").strip()
                mail_zip   = str(row.get("mail_zip",   "") or "").strip()
                yr_built   = str(row.get("yr_impr",    "") or "").strip()
                living_area= str(row.get("impr_sqft",  "") or "").strip()
                bedrooms   = str(row.get("bedrooms",   "") or "").strip()
                bathrooms  = str(row.get("bathrooms",  "") or "").strip()
                deed_date  = str(row.get("deed_date",  "") or "").strip()

                prop_address = f"{situs_num} {situs_st}".strip()

                record = {
                    "prop_address": prop_address,
                    "prop_city":    situs_city,
                    "prop_state":   "TX",
                    "prop_zip":     situs_zip,
                    "mail_address": mail_addr,
                    "mail_city":    mail_city,
                    "mail_state":   mail_state,
                    "mail_zip":     mail_zip,
                    "owner":        owner,
                    "yr_built":     yr_built,
                    "living_area":  living_area,
                    "bedrooms":     bedrooms,
                    "bathrooms":    bathrooms,
                    "deed_date":    deed_date,
                }

                # Index by owner name variants
                if owner:
                    for variant in _name_variants(owner):
                        parcel_by_owner[variant] = record

                # Index by address for direct lookup
                if prop_address and situs_city:
                    addr_key = f"{prop_address.upper()} {situs_city.upper()}"
                    parcel_by_address[addr_key] = record

            total_loaded += len(rows)
            log.info(f"  Loaded {total_loaded} parcels so far...")

            if len(rows) < CCAD_SOCRATA_LIMIT:
                break
            offset += CCAD_SOCRATA_LIMIT

        log.info(f"CCAD parcels loaded: {total_loaded} records, "
                 f"{len(parcel_by_owner)} owner variants, "
                 f"{len(parcel_by_address)} addresses")

    except Exception as e:
        log.warning(f"CCAD parcel load failed: {e}")

    # Store address lookup globally for offer engine use
    _save_address_lookup(parcel_by_address)

    return parcel_by_owner


def _save_address_lookup(lookup: dict):
    """Save address lookup to JSON for use by offer engine."""
    try:
        out_file = DATA_DIR / "ccad_address_lookup.json"
        with open(out_file, "w") as f:
            json.dump(lookup, f)
        log.info(f"Address lookup saved: {len(lookup)} entries → {out_file}")
    except Exception as e:
        log.warning(f"Could not save address lookup: {e}")


def enrich_record(record: dict, parcel_by_owner: dict) -> dict:
    """Enrich a lead record with parcel data via owner name lookup."""
    owner = record.get("owner", "").upper().strip()
    if not owner:
        return record

    for variant in _name_variants(owner):
        parcel = parcel_by_owner.get(variant)
        if parcel:
            record.update({
                "prop_address": parcel["prop_address"],
                "prop_city":    parcel["prop_city"],
                "prop_state":   parcel["prop_state"],
                "prop_zip":     parcel["prop_zip"],
                "mail_address": parcel["mail_address"],
                "mail_city":    parcel["mail_city"],
                "mail_state":   parcel["mail_state"],
                "mail_zip":     parcel["mail_zip"],
            })
            break
    return record


# ─────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────

def score_record(record: dict) -> tuple[int, list[str]]:
    flags = [label for label, pred in FLAG_DEFS if pred(record)]
    score = 30
    score += min(len(flags) * 10, 40)
    if record["cat"] in ("LP",) and any(
        r["cat"] in ("NOFC","TAXDEED") for r in [record]
    ):
        score += 20
    try:
        amt = float(str(record.get("amount", "0")).replace(",","").replace("$","") or 0)
        if amt > 100000: score += 15
        elif amt > 50000: score += 10
    except: pass
    if record.get("prop_address"): score += 5
    return min(score, 100), flags


# ─────────────────────────────────────────────────────────────
# Collin County Clerk — Lis Pendens & Foreclosures
# ─────────────────────────────────────────────────────────────

async def fetch_clerk_records(start_date: str, end_date: str) -> list[dict]:
    """
    Fetch LP and NOFC records from Collin County Clerk portal.
    Uses Playwright to handle JavaScript-rendered search.
    """
    records = []
    log.info(f"Fetching Collin County Clerk records {start_date} → {end_date}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx     = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await ctx.new_page()

        try:
            # Collin County uses a different portal than Dallas
            # Try the official records search
            await page.goto(CLERK_SEARCH_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=20000)

            # Check for search form
            content = await page.content()
            log.info(f"Clerk portal loaded, content length: {len(content)}")

            # Look for search inputs
            inputs = await page.query_selector_all("input[type='text'], input[type='date'], select")
            log.info(f"Found {len(inputs)} input elements on clerk portal")

            # Try to find and fill date range search
            # Collin County clerk portal structure varies — attempt common patterns
            doc_types_to_search = ["LIS PENDENS", "NOTICE OF FORECLOSURE", "PROBATE"]

            for doc_type in doc_types_to_search:
                try:
                    # Try direct URL search if available
                    search_params = {
                        "docType": doc_type,
                        "startDate": start_date,
                        "endDate": end_date,
                    }
                    log.info(f"Searching for: {doc_type}")
                    # Portal-specific logic would go here based on actual HTML structure
                    # For now log the attempt
                    await asyncio.sleep(1)
                except Exception as e:
                    log.warning(f"Doc type search failed for {doc_type}: {e}")

        except Exception as e:
            log.warning(f"Clerk portal error: {e}")
        finally:
            await browser.close()

    log.info(f"Clerk records fetched: {len(records)}")
    return records


# ─────────────────────────────────────────────────────────────
# re:SearchTX — Collin County Probate
# ─────────────────────────────────────────────────────────────

async def fetch_probate_records(start_date: str, end_date: str) -> list[dict]:
    """
    Fetch probate cases from re:SearchTX for Collin County.
    Same platform as Dallas — just different jurisdiction.
    """
    records = []
    if not RESEARCH_TX_COOKIE:
        log.warning("RESEARCH_TX_COOKIE not set — skipping probate")
        return records

    log.info(f"Fetching Collin County probate {start_date} → {end_date}")

    headers = {
        "Cookie":       RESEARCH_TX_COOKIE,
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "User-Agent":   "Mozilla/5.0",
        "Referer":      RESEARCH_TX_BASE,
    }

    # Collin County jurisdiction IDs on re:SearchTX
    # County Court at Law (probate jurisdiction in Collin County)
    jurisdictions = [
        {"id": 5718, "key": "collin:ccl1", "name": "Collin County Court at Law 1"},
        {"id": 5719, "key": "collin:ccl2", "name": "Collin County Court at Law 2"},
        {"id": 5720, "key": "collin:ccl3", "name": "Collin County Court at Law 3"},
        {"id": 5721, "key": "collin:ccl4", "name": "Collin County Court at Law 4"},
        {"id": 5722, "key": "collin:prob", "name": "Collin County Probate Court"},
    ]

    for jur in jurisdictions:
        for page_num in range(0, 5):
            try:
                payload = {
                    "jurisdictionId":  jur["id"],
                    "caseCategory":    "Probate",
                    "filedDateFrom":   start_date,
                    "filedDateTo":     end_date,
                    "page":            page_num,
                    "pageSize":        99,
                }
                resp = requests.post(
                    f"{RESEARCH_TX_SEARCH}/api/cases",
                    headers=headers,
                    json=payload,
                    timeout=30
                )

                if resp.status_code == 401:
                    log.warning("re:SearchTX cookie expired")
                    print("cookie expired")
                    return records

                if resp.status_code != 200:
                    log.warning(f"Probate search {jur['name']} status {resp.status_code}")
                    break

                data = resp.json()
                hits = data.get("hits", data.get("cases", []))
                if not hits:
                    break

                log.info(f"Probate {jur['name']} page {page_num}: {len(hits)} cases")

                for case in hits:
                    desc = case.get("description", "") or ""
                    owner = _extract_probate_name(desc)
                    records.append({
                        "doc_num":   case.get("caseNumber", ""),
                        "doc_type":  "PRO",
                        "filed":     case.get("dateFiled", "")[:10] if case.get("dateFiled") else "",
                        "cat":       "PRO",
                        "cat_label": "Probate",
                        "owner":     owner,
                        "grantee":   "",
                        "amount":    "",
                        "legal":     desc,
                        "prop_address": "", "prop_city": "", "prop_state": "TX", "prop_zip": "",
                        "mail_address": "", "mail_city": "", "mail_state": "", "mail_zip": "",
                        "clerk_url": f"{RESEARCH_TX_BASE}/CaseDetail/{case.get('caseDataID','')}",
                        "flags": [], "score": 30,
                    })

                if len(hits) < 99:
                    break

            except Exception as e:
                log.warning(f"Probate {jur['name']} page {page_num} error: {e}")
                break

    log.info(f"Probate records: {len(records)}")
    return records


def _extract_probate_name(description: str) -> str:
    """Extract person name from probate case description."""
    desc = description.upper()
    patterns = [
        r"IN RE[:\s]+THE ESTATE OF ([A-Z][A-Z\s,\.]+?),?\s*DECEASED",
        r"ESTATE OF ([A-Z][A-Z\s,\.]+?),?\s*DECEASED",
        r"IN RE[:\s]+ESTATE OF ([A-Z][A-Z\s,\.]+?)(?:,|\s+A/K/A|\s+DECEASED|$)",
        r"IN THE MATTER OF THE ESTATE OF ([A-Z][A-Z\s,\.]+?),?\s*DECEASED",
    ]
    for pat in patterns:
        m = re.search(pat, desc)
        if m:
            name = m.group(1).strip().rstrip(",").strip()
            name = re.sub(r"\s+", " ", name)
            if 3 < len(name) < 60:
                return name
    return description[:80]


# ─────────────────────────────────────────────────────────────
# Collin County Foreclosure notices (alternative source)
# ─────────────────────────────────────────────────────────────

def fetch_foreclosure_notices(start_date: str, end_date: str) -> list[dict]:
    """
    Fetch foreclosure notices posted by Collin County.
    Collin County posts notices at collincountytx.gov/constable
    """
    records = []
    log.info(f"Fetching Collin County foreclosure notices...")

    try:
        # Collin County foreclosure notice search
        urls_to_try = [
            "https://www.collincountytx.gov/constable/foreclosure_notices",
            "https://www.collincountytx.gov/county_clerk/foreclosure_sales",
        ]

        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

        for url in urls_to_try:
            try:
                resp = session.get(url, timeout=15)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "lxml")
                    log.info(f"Foreclosure page loaded: {url}")

                    # Extract table rows or list items with foreclosure data
                    tables = soup.find_all("table")
                    for table in tables:
                        rows = table.find_all("tr")
                        for row in rows[1:]:  # Skip header
                            cells = row.find_all(["td", "th"])
                            if len(cells) >= 3:
                                records.append({
                                    "doc_num":   cells[0].get_text(strip=True) if cells else "",
                                    "doc_type":  "NOFC",
                                    "filed":     cells[1].get_text(strip=True) if len(cells) > 1 else "",
                                    "cat":       "NOFC",
                                    "cat_label": "Notice of Foreclosure",
                                    "owner":     cells[2].get_text(strip=True) if len(cells) > 2 else "",
                                    "grantee":   "",
                                    "amount":    cells[3].get_text(strip=True) if len(cells) > 3 else "",
                                    "legal":     "",
                                    "prop_address": "", "prop_city": "", "prop_state": "TX", "prop_zip": "",
                                    "mail_address": "", "mail_city": "", "mail_state": "", "mail_zip": "",
                                    "clerk_url": url,
                                    "flags": [], "score": 30,
                                })
                    break
            except Exception as e:
                log.warning(f"Foreclosure URL {url}: {e}")

    except Exception as e:
        log.warning(f"Foreclosure fetch error: {e}")

    log.info(f"Foreclosure notices: {len(records)}")
    return records


# ─────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────

def save_outputs(records: list[dict], date_range: dict):
    """Save records.json and GHL CSV — same format as DCAD."""
    now = datetime.now(timezone.utc).isoformat()
    with_address = sum(1 for r in records if r.get("prop_address"))

    payload = {
        "fetched_at":  now,
        "source":      "Collin County",
        "date_range":  date_range,
        "total":       len(records),
        "with_address": with_address,
        "records":     records,
    }

    for out_dir in [DATA_DIR, DASHBOARD_DIR]:
        with open(out_dir / "records.json", "w") as f:
            json.dump(payload, f, indent=2, default=str)
    log.info(f"Saved: {DATA_DIR}/records.json")
    log.info(f"Saved: {DASHBOARD_DIR}/records.json")

    # GHL CSV export
    today = datetime.now().strftime("%Y%m%d")
    ghl_file = DATA_DIR / f"ghl_export_{today}.csv"
    fieldnames = [
        "First Name", "Last Name", "Mailing Address", "Mailing City",
        "Mailing State", "Mailing Zip", "Property Address", "Property City",
        "Property State", "Property Zip", "Lead Type", "Document Type",
        "Date Filed", "Document Number", "Amount/Debt Owed", "Seller Score",
        "Motivated Seller Flags", "Source", "Public Records URL",
    ]

    with open(ghl_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            name_parts = r.get("owner","").split()
            first = name_parts[0].title() if name_parts else ""
            last  = " ".join(name_parts[1:]).title() if len(name_parts) > 1 else ""
            writer.writerow({
                "First Name":           first,
                "Last Name":            last,
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
    log.info(f"━━━ Complete: {len(records)} records ({with_address} with address)")


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

async def main():
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str   = end_dt.strftime("%Y-%m-%d")
    date_range = {"start": start_str, "end": end_str}

    log.info(f"Collin County scraper starting: {start_str} → {end_str}")

    # Load parcel data
    parcel_by_owner = load_parcel_data()

    # Fetch leads
    clerk_records       = await fetch_clerk_records(start_str, end_str)
    probate_records     = await fetch_probate_records(start_str, end_str)
    foreclosure_records = fetch_foreclosure_notices(start_str, end_str)

    all_records = clerk_records + probate_records + foreclosure_records

    log.info(f"Raw totals — Clerk: {len(clerk_records)} | "
             f"Probate: {len(probate_records)} | "
             f"Foreclosure: {len(foreclosure_records)}")

    # Enrich + score
    final = []
    for rec in all_records:
        try:
            rec = enrich_record(rec, parcel_by_owner)
            rec["score"], rec["flags"] = score_record(rec)
            final.append(rec)
        except Exception as e:
            log.warning(f"Enrich failed for {rec.get('doc_num','?')}: {e}")

    save_outputs(final, date_range)


if __name__ == "__main__":
    asyncio.run(main())
