#!/usr/bin/env python3
"""
Acquis Deal Flow Scraper v2.2
Scrapes French M&A platforms daily for business acquisition opportunities
matching Lughanor's target criteria.

Sources (confirmed working):
- Transentreprise.com      — POST search, parse div.row.mb-3 cards
- BPI France Transmission  — GET production?searchText=KEYWORD, parse article.result
"""

import json
import time
import re
import hashlib
import os
import sys
from datetime import datetime, timedelta
from typing import Optional
import requests
from bs4 import BeautifulSoup

# ─── Target Criteria ───────────────────────────────────────────────────────────

# Tier 1 — strong signal: title MUST contain at least one of these
SECTOR_KEYWORDS_TITLE = [
    "petfood", "pet food",
    "alimentation animale", "nutrition animale", "aliment animal", "aliments animaux",
    "nourriture animale", "nourriture pour animaux",
    "croquettes", "pâtée",
    "ruminant", "bovin", "ovin", "caprin", "volaille", "volailles", "aviculture",
    "provenderie", "fabrication aliments", "aliments du bétail",
    "nac", "nouveaux animaux de compagnie", "reptile", "aquariophilie",
    "équin", "équine", "équitation", "cheval", "chevaux", "hippique",
    "nutrition équine", "aliment cheval",
    "animalerie", "jardinerie animalerie",
    "élevage porcin", "porcin", "porc",
    "friandise",  # friandises pour animaux
    "snack chien", "snack chat",
]

# Tier 2 — broader, checked in title+description (animal-specific only, no generic food terms)
SECTOR_KEYWORDS_BROAD = SECTOR_KEYWORDS_TITLE + [
    "élevage",              # animal farming context
    "produits vétérinaires", "vétérinaire", "soins animaux", "accessoires animaux",
    "bien-être animal", "bienêtre animal",
    "aquaculture", "pisciculture",
    "apiculture", "ruche",
]

CA_MIN = 300_000    # légèrement sous 500K pour ne pas rater les borderlines
CA_MAX = 7_000_000  # légèrement au-dessus de 5M

MAX_AGE_DAYS = 62   # ~2 mois

# Path relative to repo root (scraper is at acquis-bot/scraper/scraper.py)
DEALS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "deals.json")
DEALS_FILE = os.path.normpath(DEALS_FILE)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
}

# ─── Utilities ─────────────────────────────────────────────────────────────────

def deal_id(url: str, title: str) -> str:
    key = f"{url.strip().lower()}{title.strip().lower()}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def matches_sector_strict(title: str) -> bool:
    """Title must contain a strong sector keyword."""
    n = normalize(title)
    return any(kw in n for kw in SECTOR_KEYWORDS_TITLE)


def matches_sector_broad(title: str, desc: str) -> bool:
    """Title OR description contains a sector keyword."""
    n = normalize(f"{title} {desc}")
    return any(kw in n for kw in SECTOR_KEYWORDS_BROAD)


def parse_ca(text: str) -> Optional[int]:
    """Parse CA strings: '1.2M€', '800 K€', '1 200 000 €', 'de 500 à 1000 k€' → int"""
    if not text:
        return None
    t = text.replace("\u202f", "").replace("\xa0", "").replace(" ", "").lower()
    range_match = re.search(r"(?:de\s*)?[\d.,]+\s*(?:à|a|-)\s*([\d.,]+)\s*([mk€])", t)
    if range_match:
        try:
            val = float(range_match.group(1).replace(",", "."))
            unit = range_match.group(2)
            if unit == "m":
                return int(val * 1_000_000)
            elif unit == "k":
                return int(val * 1_000)
        except ValueError:
            pass
    m = re.search(r"([\d.,]+)\s*m[€e]?", t)
    if m:
        try:
            return int(float(m.group(1).replace(",", ".")) * 1_000_000)
        except ValueError:
            pass
    m = re.search(r"([\d.,]+)\s*k[€e]?", t)
    if m:
        try:
            return int(float(m.group(1).replace(",", ".")) * 1_000)
        except ValueError:
            pass
    m = re.search(r"(\d[\d\s]*\d)[€e]", t)
    if m:
        try:
            return int(m.group(1).replace(" ", ""))
        except ValueError:
            pass
    return None


def ca_in_range(ca: Optional[int]) -> bool:
    if ca is None:
        return True
    return CA_MIN <= ca <= CA_MAX


def get_soup(session: requests.Session, url: str, retries: int = 2,
             method: str = "GET", data: dict = None) -> Optional[BeautifulSoup]:
    for attempt in range(retries):
        try:
            if method == "POST":
                resp = session.post(url, data=data, timeout=25,
                                    allow_redirects=True, verify=False)
            else:
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
    if os.path.exists(DEALS_FILE):
        try:
            with open(DEALS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return {d["id"]: d for d in data.get("deals", [])}
        except Exception as e:
            print(f"  Warning: could not load existing deals: {e}")
    return {}


def save_deals(deals_by_id: dict):
    deals_list = list(deals_by_id.values())
    deals_list.sort(key=lambda d: (d.get("date_scraped", ""), d.get("source", "")), reverse=True)
    cutoff = (datetime.utcnow() - timedelta(days=120)).date().isoformat()
    deals_list = [d for d in deals_list if d.get("date_scraped", "9999") >= cutoff]
    payload = {
        "last_updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(deals_list),
        "new_today": sum(1 for d in deals_list
                         if d.get("date_scraped") == datetime.utcnow().date().isoformat()),
        "deals": deals_list,
    }
    with open(DEALS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n✓ deals.json updated — {len(deals_list)} total deals")


def make_deal(title, url, source, region="", ca_text="", desc="", date_pub="") -> dict:
    ca_val = parse_ca(ca_text + " " + desc)
    return {
        "id": deal_id(url, title),
        "title": title.strip(),
        "url": url.strip(),
        "source": source,
        "region": region.strip(),
        "ca": ca_text.strip() or "NC",
        "ca_value": ca_val,
        "description": desc[:400].strip() if desc else "",
        "date_pub": date_pub.strip(),
        "date_scraped": datetime.utcnow().date().isoformat(),
        "seen": False,
        "added_to_crm": False,
    }


# ─── Scraper: Transentreprise ───────────────────────────────────────────────────

def scrape_transentreprise(existing: dict, session: requests.Session) -> list:
    """
    Transentreprise.com — POST search to set server session, then parse results.
    Card structure: div.row.mb-3 > [div.col-sm-5 (image), div.col-sm-7 (details)]
    """
    new_deals = []
    base = "https://www.transentreprise.com"
    search_url = f"{base}/offres/newsearch"
    seen_ids = set()

    keywords = [
        "alimentation animale",
        "nutrition animale",
        "petfood",
        "animalerie",
        "provenderie",
        "volailles élevage",
        "équin cheval",
        "élevage bovin",
    ]

    for kw in keywords:
        print(f"    🔍 Searching: {kw}")
        soup = get_soup(session, search_url, method="POST", data={
            "int-activitie": kw,
            "int-localisations": "",
            "nouv": "31",
            "json": "",
            "filtre": "",
        })
        if not soup:
            time.sleep(2)
            continue

        cards = soup.select("div.row.mb-3")
        if not cards:
            time.sleep(2)
            continue

        for card in cards:
            fiche_links = [a for a in card.select("a[href*='/offres/fiche/']") if len(a.get_text(strip=True)) > 4]
            if not fiche_links:
                continue

            title_el = fiche_links[0]
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            if href and not href.startswith("http"):
                href = base + href

            detail = card.select_one("div.col-sm-7, div.col-12.col-sm-7")

            region = ""
            if detail:
                region_el = detail.select_one("p a, span.region, .localisation")
                if not region_el:
                    all_text_parts = [p.get_text(strip=True) for p in detail.select("p")]
                    region = all_text_parts[1] if len(all_text_parts) > 1 else ""
                else:
                    region = region_el.get_text(strip=True)

            ca_txt = ""
            if detail:
                full_text = detail.get_text(separator="\n")
                ca_match = re.search(r"C\.A\.\s*:?\s*(.+?)(?:\n|Effectif|$)", full_text, re.IGNORECASE)
                if ca_match:
                    ca_txt = ca_match.group(1).strip()

            date_pub = ""
            if detail:
                date_match = re.search(r"\d{2}/\d{2}/\d{4}", detail.get_text())
                if date_match:
                    date_pub = date_match.group(0)

            desc = detail.get_text(separator=" ", strip=True)[:400] if detail else ""

            if not title or len(title) < 5:
                continue

            # Strict: title must match a sector keyword
            if not matches_sector_strict(title):
                if not matches_sector_broad(title, desc):
                    continue

            if not ca_in_range(parse_ca(ca_txt + " " + desc)):
                continue

            did = deal_id(href or title, title)
            if did in existing or did in seen_ids:
                continue

            seen_ids.add(did)
            deal = make_deal(title, href or search_url, "Transentreprise",
                             region, ca_txt, desc, date_pub)
            new_deals.append(deal)
            print(f"    ✚ {title[:65]}")

        time.sleep(3)

    return new_deals


# ─── Scraper: BPI France Transmission ──────────────────────────────────────────

def scrape_bpi(existing: dict, session: requests.Session) -> list:
    """
    reprise-entreprise.bpifrance.fr — Bourse de la Transmission
    URL: /production?searchText=KEYWORD  or  /commerce?searchText=KEYWORD
    Card: article.result  |  title: h3 a  |  link: a.info-annonce
    """
    new_deals = []
    base = "https://reprise-entreprise.bpifrance.fr"
    seen_ids = set()

    search_configs = [
        ("production", "alimentation animale"),
        ("production", "petfood"),
        ("production", "nutrition animale"),
        ("production", "volailles"),
        ("production", "élevage"),
        ("production", "provenderie"),
        ("production", "équin"),
        ("commerce",   "animalerie"),
        ("commerce",   "alimentation animale"),
    ]

    for section, kw in search_configs:
        url = f"{base}/{section}?searchText={requests.utils.quote(kw)}"
        print(f"    🔍 BPI {section}: {kw}")
        soup = get_soup(session, url)
        if not soup:
            time.sleep(2)
            continue

        cards = soup.select("article.result")
        if not cards:
            time.sleep(2)
            continue

        for card in cards:
            title_els = card.select("h3 a, h2 a")
            title = ""
            for tel in title_els:
                t = tel.get_text(strip=True)
                if len(t) > 5:
                    title = t
                    break

            href = ""
            direct_link = card.select_one("a.info-annonce, a[href*='/annonce/']")
            if direct_link:
                href = direct_link.get("href", "")
            if not href:
                tracking = card.select_one("a.link.to-track, a[href*='/tracking/']")
                if tracking:
                    href = tracking.get("href", "")
            if href and not href.startswith("http"):
                href = base + href

            region = ""
            region_el = card.select_one(".departement, .region, .localisation, "
                                        "[class*='depart'], [class*='region']")
            if region_el:
                region = region_el.get_text(strip=True)

            ca_txt = ""
            ca_el = card.select_one("[class*='ca'], [class*='CA'], td.ca")
            if ca_el:
                ca_txt = ca_el.get_text(strip=True)
            if card.get("data-prix"):
                px = int(card["data-prix"])
                if px > 0:
                    ca_txt = ca_txt or f"{px // 1000} k€"

            date_pub = ""
            date_el = card.select_one(".date, time, [class*='date']")
            if date_el:
                date_pub = date_el.get_text(strip=True)
            else:
                dm = re.search(r"\d{2}/\d{2}/\d{4}", card.get_text())
                if dm:
                    date_pub = dm.group(0)

            desc = card.get_text(separator=" ", strip=True)[:400]

            if not title or len(title) < 5:
                continue
            if not matches_sector_strict(title):
                if not matches_sector_broad(title, desc):
                    continue
            if not ca_in_range(parse_ca(ca_txt + " " + desc)):
                continue

            did = deal_id(href or title, title)
            if did in existing or did in seen_ids:
                continue

            seen_ids.add(did)
            deal = make_deal(title, href or url, "BPI France",
                             region, ca_txt, desc, date_pub)
            new_deals.append(deal)
            print(f"    ✚ {title[:65]}")

        time.sleep(2)

    return new_deals


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    print(f"\n{'='*65}")
    print(f"  🔍 Acquis Deal Flow Scraper v2.1 — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Sectors: petfood | nutrition animale | équin | NAC | volailles")
    print(f"  CA range: {CA_MIN//1000}K€ — {CA_MAX//1_000_000}M€")
    print(f"  Deals file: {DEALS_FILE}")
    print(f"{'='*65}\n")

    existing = load_existing()
    print(f"  Existing deals in database: {len(existing)}\n")

    session = requests.Session()
    session.headers.update(HEADERS)

    all_new = []

    scrapers = [
        ("Transentreprise", lambda e: scrape_transentreprise(e, session)),
        ("BPI France",      lambda e: scrape_bpi(e, session)),
        ("Fusacq",          lambda e: scrape_fusacq(e, session)),
    ]

    for name, fn in scrapers:
        print(f"▶ {name}...")
        try:
            new = fn(existing)
            all_new.extend(new)
            print(f"  → {len(new)} new deal(s) found")
        except Exception as e:
            print(f"  ✗ {name} failed: {e}")
        print()

    print(f"{'='*65}")
    print(f"  Total new: {len(all_new)} deal(s)")

    for deal in all_new:
        existing[deal["id"]] = deal

    save_deals(existing)

    if all_new:
        print(f"\n🆕 New opportunities today:")
        for d in sorted(all_new, key=lambda x: x["source"]):
            ca_str = d["ca"] if d["ca"] != "NC" else "CA?"
            print(f"  [{d['source']:15s}] {d['title'][:50]:50s} | "
                  f"{d['region']:15s} | {ca_str}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
