#!/usr/bin/env python3
"""
Acquis Job Flow Scraper v1.0 — REFONTE 30/04/2026

Scrapes job boards 2x/day for senior procurement / industrial leadership
roles in Lughanor's geographic priority zones (dépt 13/68/67/25 + remote).

Sources:
- HelloWork (search RSS-friendly URL)
- Indeed (RSS / search HTML)
- France Travail (Pôle Emploi public API — stub, requires API key)
- LinkedIn (stub — anti-bot blocks scraping; manual import only)

Output: jobs.json at repo root.
"""

import json
import time
import re
import hashlib
import os
import sys
from datetime import datetime, timedelta
from typing import Optional, List
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus

# ─── Target Criteria ─────────────────────────────────────

JOB_TITLE_KEYWORDS = [
    "directeur achats", "directrice achats", "direction achats",
    "head of procurement", "chief procurement officer", "cpo",
    "vp procurement", "purchasing director", "purchasing manager",
    "responsable achats", "manager achats",
    "directeur supply chain", "directrice supply chain",
    "directeur général", "general manager", "managing director",
    "directeur industriel", "industrial director", "directeur usine",
    "plant manager",
]

# Department codes to search (Lughanor focus geography)
TARGET_DEPTS = {
    "13": ["bouches-du-rhône", "marseille", "aix-en-provence"],
    "67": ["bas-rhin", "strasbourg"],
    "68": ["haut-rhin", "mulhouse", "colmar"],
    "25": ["doubs", "besançon", "montbéliard"],
}

# Path relative to repo root
JOBS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "jobs.json")
JOBS_FILE = os.path.normpath(JOBS_FILE)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MAX_AGE_DAYS = 30


# ─── Utilities ─────────────────────────────────────────────────────────────────

def job_id(url: str, title: str, company: str) -> str:
    key = f"{url.strip().lower()}{title.strip().lower()}{company.strip().lower()}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def matches_title_keyword(title: str) -> bool:
    n = normalize(title)
    return any(kw in n for kw in JOB_TITLE_KEYWORDS)


def detect_dept(text: str) -> str:
    """Returns department code (13/67/68/25) or empty."""
    n = normalize(text)
    for dept, kws in TARGET_DEPTS.items():
        if any(kw in n for kw in kws) or re.search(rf"\b{dept}\b", n):
            return dept
    return ""


def get_soup(session: requests.Session, url: str, retries: int = 2) -> Optional[BeautifulSoup]:
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=25, allow_redirects=True, verify=False)
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, "lxml")
            print(f"    HTTP {resp.status_code} — {url}")
        except Exception as e:
            print(f"    Error fetching {url}: {e}")
        if attempt < retries - 1:
            time.sleep(3)
    return None


def load_existing() -> dict:
    if os.path.exists(JOBS_FILE):
        try:
            with open(JOBS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return {j["id"]: j for j in data.get("jobs", [])}
        except Exception as e:
            print(f"  Warning: could not load existing jobs: {e}")
    return {}


def save_jobs(jobs_by_id: dict):
    jobs_list = list(jobs_by_id.values())
    jobs_list.sort(key=lambda j: (j.get("date_scraped", ""), j.get("source", "")), reverse=True)
    cutoff = (datetime.utcnow() - timedelta(days=60)).date().isoformat()
    jobs_list = [j for j in jobs_list if j.get("date_scraped", "9999") >= cutoff]
    payload = {
        "last_updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(jobs_list),
        "new_today": sum(1 for j in jobs_list
                         if j.get("date_scraped") == datetime.utcnow().date().isoformat()),
        "jobs": jobs_list,
    }
    with open(JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n✓ jobs.json updated — {len(jobs_list)} total jobs")


def make_job(title, url, source, company="", location="", dept="", salary="", summary="") -> dict:
    if not dept:
        dept = detect_dept(f"{title} {company} {location}")
    return {
        "id": job_id(url, title, company),
        "title": title.strip(),
        "url": url.strip(),
        "source": source,
        "company": company.strip(),
        "location": location.strip(),
        "dept": dept,
        "salary": salary.strip() if salary else "",
        "summary": (summary or "").strip()[:400],
        "date_scraped": datetime.utcnow().date().isoformat(),
        "date_scraped_iso": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seen": False,
        "added_to_pipeline": False,
    }


# ─── Scraper: HelloWork ─────────────────────────────────────

def scrape_hellowork(existing: dict, session: requests.Session) -> List[dict]:
    """HelloWork — search by keyword + dept code."""
    new_jobs = []
    base_search = "https://www.hellowork.com/fr-fr/emploi/recherche.html"

    for dept_code, dept_names in TARGET_DEPTS.items():
        for kw in ["directeur achats", "responsable achats", "directeur industriel"]:
            params = f"?k={quote_plus(kw)}&l={quote_plus(dept_names[0])}"
            url = base_search + params
            soup = get_soup(session, url)
            if not soup:
                continue

            cards = soup.select("li[data-cy='serpCard'], article.tw-cursor-pointer, div[data-id-job]")
            for card in cards[:10]:
                a = card.select_one("a[href*='/emploi/']") or card.select_one("a")
                if not a:
                    continue
                href = a.get("href", "")
                if not href:
                    continue
                if href.startswith("/"):
                    href = "https://www.hellowork.com" + href

                title = (a.get_text() or "").strip()[:120]
                if not title or not matches_title_keyword(title):
                    continue

                company_el = card.select_one("[data-cy='companyName'], .tw-text-grey")
                company = company_el.get_text(strip=True) if company_el else ""
                loc_el = card.select_one("[data-cy='localisationCard'], .tw-text-grey-7")
                location = loc_el.get_text(strip=True) if loc_el else dept_names[0]

                jid = job_id(href, title, company)
                if jid in existing:
                    continue

                job = make_job(title, href, "HelloWork", company, location, dept_code)
                new_jobs.append(job)
                existing[jid] = job

            time.sleep(2)
    return new_jobs


# ─── Scraper: Indeed ─────────────────────────────────────

def scrape_indeed(existing: dict, session: requests.Session) -> List[dict]:
    """Indeed France — search by keyword + dept. May trigger anti-bot."""
    new_jobs = []
    base_search = "https://fr.indeed.com/jobs"

    for dept_code, dept_names in TARGET_DEPTS.items():
        for kw in ["directeur achats", "directeur industriel"]:
            params = f"?q={quote_plus(kw)}&l={quote_plus(dept_names[0])}&radius=25"
            url = base_search + params
            soup = get_soup(session, url)
            if not soup:
                continue

            cards = soup.select("div.job_seen_beacon, div.cardOutline, li.css-1ac2h1w")
            for card in cards[:10]:
                a = card.select_one("a.jcs-JobTitle, h2 a")
                if not a:
                    continue
                href = a.get("href", "")
                if href.startswith("/"):
                    href = "https://fr.indeed.com" + href

                title = (a.get_text() or "").strip()[:120]
                if not title or not matches_title_keyword(title):
                    continue

                company_el = card.select_one("[data-testid='company-name'], .companyName")
                company = company_el.get_text(strip=True) if company_el else ""
                loc_el = card.select_one("[data-testid='text-location'], .companyLocation")
                location = loc_el.get_text(strip=True) if loc_el else dept_names[0]
                sal_el = card.select_one(".salary-snippet, [data-testid='attribute_snippet_testid']")
                salary = sal_el.get_text(strip=True) if sal_el else ""

                jid = job_id(href, title, company)
                if jid in existing:
                    continue

                job = make_job(title, href, "Indeed", company, location, dept_code, salary)
                new_jobs.append(job)
                existing[jid] = job

            time.sleep(3)  # Indeed = sensible
    return new_jobs


# ─── Scraper: LinkedIn (stub — anti-bot) ─────────────────────────────────────

def scrape_linkedin(existing: dict, session: requests.Session) -> List[dict]:
    """LinkedIn jobs — public search blocked by anti-bot. Stub for v1."""
    print("    (stub — LinkedIn requires authenticated API; manual import recommended)")
    return []


# ─── Scraper: France Travail / Pôle Emploi (stub — needs API key) ────────────

def scrape_france_travail(existing: dict, session: requests.Session) -> List[dict]:
    """France Travail public API — requires registered API key (free tier available)."""
    print("    (stub — France Travail API requires registration at francetravail.io)")
    return []


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    print(f"\n{'='*65}")
    print(f"  💼 Acquis Job Flow Scraper v1.0 — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Target roles: Direction Achat / Industriel / Supply Chain")
    print(f"  Departments: {', '.join(TARGET_DEPTS.keys())}")
    print(f"  Output: {JOBS_FILE}")
    print(f"{'='*65}\n")

    existing = load_existing()
    print(f"  Existing jobs in database: {len(existing)}\n")

    session = requests.Session()
    session.headers.update(HEADERS)

    all_new = []

    scrapers = [
        ("HelloWork",       lambda e: scrape_hellowork(e, session)),
        ("Indeed",          lambda e: scrape_indeed(e, session)),
        ("LinkedIn",        lambda e: scrape_linkedin(e, session)),
        ("France Travail",  lambda e: scrape_france_travail(e, session)),
    ]

    for name, fn in scrapers:
        print(f"▶ {name}...")
        try:
            new = fn(existing)
            all_new.extend(new)
            print(f"  → {len(new)} new job(s) found")
        except Exception as e:
            print(f"  ✗ {name} failed: {e}")
        print()

    print(f"{'='*65}")
    print(f"  Total new: {len(all_new)} job(s)")

    save_jobs(existing)

    if all_new:
        print(f"\n🆕 New roles today:")
        for j in sorted(all_new, key=lambda x: (x.get("dept", "ZZ"), x["source"])):
            print(f"  [{j.get('dept', '--')}] [{j['source']:10s}] {j['title'][:50]:50s} | "
                  f"{j['company'][:25]:25s} | {j['location'][:25]:25s}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
