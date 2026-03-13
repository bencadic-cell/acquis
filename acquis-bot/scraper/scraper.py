#!/usr/bin/env python3
"""
Acquis Deal Flow Scraper
Scrapes French M&A platforms daily for business acquisition opportunities
matching Lughanor's target criteria.

Targets:
- Transentreprise.com
- BPI France Transmission (reprise-entreprise.bpifrance.fr)
- Fusacq.com
- CRA (cra.asso.fr)
- Cessionpme.com
- MaCessionEntreprise.fr
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

SECTOR_KEYWORDS = [
    # Petfood / alimentation animale
    "petfood", "pet food", "alimentation animale", "nutrition animale",
    "aliment animal", "aliments animaux", "nourriture animale",
    "nourriture pour animaux", "croquettes", "pâtée", "pâtée pour",
    # Ruminants / élevage
    "ruminant", "bovin", "bovins", "ovin", "ovins", "caprin", "caprins",
    "volaille", "volailles", "aviculture", "avicole", "poulet", "dindon",
    "provenderie", "fabrication aliments", "aliments du bétail",
    "nutrition bovine", "nutrition ovine", "élevage porcin",
    # NAC
    "nac", "nouveaux animaux de compagnie", "animaux exotiques",
    "reptile", "reptiles", "oiseau de compagnie", "oiseaux exotiques",
    "aquariophilie", "aquarium", "poisson tropical",
    # Équin
    "équin", "équine", "équitation", "cheval", "chevaux", "hippique",
    "nutrition équine", "aliment cheval",
    # Animalerie / distribution
    "animalerie", "jardinerie animalerie", "soins animaux", "accessoires animaux",
    "vétérinaire", "produits vétérinaires",
    # Agro-alimentaire (filtre large)
    "agroalimentaire", "agro-alimentaire", "industrie alimentaire",
    "fabrication alimentaire", "transformation alimentaire",
]

CA_MIN = 300_000    # légèrement en dessous de 500K pour ne pas rater les borderlines
CA_MAX = 7_000_000  # légèrement au-dessus de 5M

MAX_AGE_DAYS = 62   # ~2 mois

DEALS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "deals.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ─── Utilities ─────────────────────────────────────────────────────────────────

def deal_id(url: str, title: str) -> str:
    key = f"{url.strip().lower()}{title.strip().lower()}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def matches_sector(text: str) -> bool:
    n = normalize(text)
    return any(kw in n for kw in SECTOR_KEYWORDS)


def parse_ca(text: str) -> Optional[int]:
    """Parse CA strings like '1.2M€', '800 K€', '1 200 000 €' → int"""
    if not text:
        return None
    t = text.replace("\u202f", "").replace("\xa0", "").replace(" ", "").lower()
    # Millions
    m = re.search(r"([\d.,]+)\s*m[€e]?", t)
    if m:
        try:
            return int(float(m.group(1).replace(",", ".")) * 1_000_000)
        except ValueError:
            pass
    # Milliers / K
    m = re.search(r"([\d.,]+)\s*k[€e]?", t)
    if m:
        try:
            return int(float(m.group(1).replace(",", ".")) * 1_000)
        except ValueError:
            pass
    # Plain number
    m = re.search(r"(\d[\d\s]*\d)[€e]", t)
    if m:
        try:
            return int(m.group(1).replace(" ", ""))
        except ValueError:
            pass
    return None


def ca_in_range(ca: Optional[int]) -> bool:
    if ca is None:
        return True  # CA inconnu → on garde (mieux vaut trop que pas assez)
    return CA_MIN <= ca <= CA_MAX


def get_soup(url: str, retries: int = 2) -> Optional[BeautifulSoup]:
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, timeout=20, allow_redirects=True)
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
    # Sort: newest first, then by source
    deals_list.sort(key=lambda d: (d.get("date_scraped", ""), d.get("source", "")), reverse=True)
    # Keep only deals younger than 4 months to avoid bloat
    cutoff = (datetime.utcnow() - timedelta(days=120)).date().isoformat()
    deals_list = [d for d in deals_list if d.get("date_scraped", "9999") >= cutoff]
    payload = {
        "last_updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(deals_list),
        "new_today": sum(1 for d in deals_list if d.get("date_scraped") == datetime.utcnow().date().isoformat()),
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

def scrape_transentreprise(existing: dict) -> list:
    """Transentreprise.com — public listings, best-structured source"""
    new_deals = []
    base = "https://www.transentreprise.com"

    # Multiple keyword searches to maximize coverage
    queries = [
        "petfood", "nutrition+animale", "alimentation+animale",
        "volailles", "élevage", "animalerie", "équin", "ruminant", "provenderie"
    ]

    for q in queries:
        url = f"{base}/offres?q={q}"
        soup = get_soup(url)
        if not soup:
            time.sleep(2)
            continue

        # Transentreprise uses cards/articles for listings
        cards = soup.select("article, .offer-card, .annonce-item, .result, .offre")
        if not cards:
            # Fallback: look for any links to /offres/ detail pages
            cards = [a.parent for a in soup.select("a[href*='/offres/']") if a.parent]

        for card in cards:
            title_el = card.select_one("h2, h3, h4, .title, .name")
            link_el  = card.select_one("a[href]")
            desc_el  = card.select_one("p, .description, .excerpt, .summary")
            region_el = card.select_one(".region, .localisation, .location, .ville, .departement")
            ca_el    = card.select_one(".ca, .chiffre, .revenue, .turnover")
            date_el  = card.select_one(".date, time, .published")

            if not (title_el or link_el):
                continue

            title  = (title_el or link_el).get_text(strip=True)
            href   = link_el.get("href", "") if link_el else ""
            if href and not href.startswith("http"):
                href = base + href
            desc   = desc_el.get_text(strip=True) if desc_el else ""
            region = region_el.get_text(strip=True) if region_el else ""
            ca_txt = ca_el.get_text(strip=True) if ca_el else ""
            date_pub = date_el.get_text(strip=True) if date_el else ""

            if not title or len(title) < 5:
                continue
            if not matches_sector(f"{title} {desc}"):
                continue
            if not ca_in_range(parse_ca(ca_txt + " " + desc)):
                continue

            did = deal_id(href or title, title)
            if did in existing:
                continue

            deal = make_deal(title, href or url, "Transentreprise", region, ca_txt, desc, date_pub)
            if did not in {d["id"] for d in new_deals}:
                new_deals.append(deal)
                print(f"    ✚ {title[:65]}")

        time.sleep(2)

    return new_deals


# ─── Scraper: BPI France Transmission ──────────────────────────────────────────

def scrape_bpi(existing: dict) -> list:
    """reprise-entreprise.bpifrance.fr — Bourse de la Transmission"""
    new_deals = []
    base = "https://reprise-entreprise.bpifrance.fr"

    # BPI has sector pages + general search
    urls_to_try = [
        f"{base}/cession-entreprise?secteur=alimentation",
        f"{base}/cession-entreprise?secteur=agriculture",
        f"{base}/cession-entreprise?secteur=commerce",
        f"{base}/cession-entreprise",
        f"{base}/annonces",
    ]

    for url in urls_to_try:
        soup = get_soup(url)
        if not soup:
            time.sleep(2)
            continue

        cards = soup.select(".annonce, .offer, .card-annonce, article, .listing-item, .result-item")
        if not cards:
            cards = soup.select("[class*='annonce'], [class*='offer'], [class*='listing']")

        for card in cards:
            title_el  = card.select_one("h2, h3, h4, .title, .nom-entreprise, strong")
            link_el   = card.select_one("a[href]")
            desc_el   = card.select_one("p, .description, .activite, .secteur-desc")
            region_el = card.select_one(".region, .localisation, .departement, .ville")
            ca_el     = card.select_one(".ca, .chiffre-affaires, [class*='ca'], [class*='revenue']")
            date_el   = card.select_one(".date, time, .publication")

            if not (title_el or link_el):
                continue

            title  = (title_el or link_el).get_text(strip=True)
            href   = link_el.get("href", "") if link_el else ""
            if href and not href.startswith("http"):
                href = base + href
            desc   = desc_el.get_text(strip=True) if desc_el else ""
            region = region_el.get_text(strip=True) if region_el else ""
            ca_txt = ca_el.get_text(strip=True) if ca_el else ""
            date_pub = date_el.get_text(strip=True) if date_el else ""

            if not title or len(title) < 5:
                continue
            if not matches_sector(f"{title} {desc}"):
                continue
            if not ca_in_range(parse_ca(ca_txt + " " + desc)):
                continue

            did = deal_id(href or title, title)
            if did in existing or did in {d["id"] for d in new_deals}:
                continue

            deal = make_deal(title, href or url, "BPI France", region, ca_txt, desc, date_pub)
            new_deals.append(deal)
            print(f"    ✚ {title[:65]}")

        time.sleep(2)
        # One successful page is enough for BPI
        if new_deals:
            break

    return new_deals


# ─── Scraper: Fusacq ───────────────────────────────────────────────────────────

def scrape_fusacq(existing: dict) -> list:
    """fusacq.com — M&A news & listings"""
    new_deals = []

    urls_to_try = [
        "https://www.fusacq.com/buzz/alimentation-animale",
        "https://www.fusacq.com/buzz/agroalimentaire",
        "https://www.fusacq.com/buzz",
        "https://search.fusacq.com/buzz",
    ]

    for url in urls_to_try:
        soup = get_soup(url)
        if not soup:
            time.sleep(2)
            continue

        items = soup.select(".buzz-item, .item, article, .news-item, .annonce, li.offer")
        if not items:
            items = soup.select("[class*='buzz'], [class*='item'], [class*='annonce']")

        for item in items:
            title_el = item.select_one("h2, h3, h4, .title, a strong, a")
            link_el  = item.select_one("a[href]")
            desc_el  = item.select_one("p, .description, .excerpt, .summary")
            date_el  = item.select_one(".date, time, .published")

            if not (title_el or link_el):
                continue

            title = (title_el or link_el).get_text(strip=True)
            href  = link_el.get("href", "") if link_el else ""
            if href and not href.startswith("http"):
                href = "https://www.fusacq.com" + href
            desc = desc_el.get_text(strip=True) if desc_el else ""
            date_pub = date_el.get_text(strip=True) if date_el else ""

            if not title or len(title) < 5:
                continue
            if not matches_sector(f"{title} {desc}"):
                continue

            did = deal_id(href or title, title)
            if did in existing or did in {d["id"] for d in new_deals}:
                continue

            deal = make_deal(title, href or url, "Fusacq", "", "", desc, date_pub)
            new_deals.append(deal)
            print(f"    ✚ {title[:65]}")

        time.sleep(2)
        if len(new_deals) > 0:
            break  # Don't hammer all fallback URLs

    return new_deals


# ─── Scraper: CRA ──────────────────────────────────────────────────────────────

def scrape_cra(existing: dict) -> list:
    """cra.asso.fr — Cédants et Repreneurs d'Affaires"""
    new_deals = []
    base = "https://www.cra.asso.fr"

    urls_to_try = [
        f"{base}/trouver-une-entreprise",
        f"{base}/annonces",
        f"{base}/offres-de-cession",
    ]

    for url in urls_to_try:
        soup = get_soup(url)
        if not soup:
            time.sleep(2)
            continue

        cards = soup.select(".annonce, .offer-card, .listing, article, .enterprise, .cession")
        if not cards:
            cards = soup.select("[class*='annonce'], [class*='offer'], [class*='cession']")

        for card in cards:
            title_el  = card.select_one("h2, h3, h4, .title, .name")
            link_el   = card.select_one("a[href]")
            desc_el   = card.select_one("p, .description, .activite")
            region_el = card.select_one(".region, .location, .ville, .departement")
            ca_el     = card.select_one(".ca, .chiffre, [class*='ca']")
            date_el   = card.select_one(".date, time")

            if not (title_el or link_el):
                continue

            title  = (title_el or link_el).get_text(strip=True)
            href   = link_el.get("href", "") if link_el else ""
            if href and not href.startswith("http"):
                href = base + href
            desc   = desc_el.get_text(strip=True) if desc_el else ""
            region = region_el.get_text(strip=True) if region_el else ""
            ca_txt = ca_el.get_text(strip=True) if ca_el else ""
            date_pub = date_el.get_text(strip=True) if date_el else ""

            if not title or len(title) < 5:
                continue
            if not matches_sector(f"{title} {desc}"):
                continue
            if not ca_in_range(parse_ca(ca_txt + " " + desc)):
                continue

            did = deal_id(href or title, title)
            if did in existing or did in {d["id"] for d in new_deals}:
                continue

            deal = make_deal(title, href or url, "CRA", region, ca_txt, desc, date_pub)
            new_deals.append(deal)
            print(f"    ✚ {title[:65]}")

        time.sleep(2)
        if new_deals:
            break

    return new_deals


# ─── Scraper: Cessionpme ───────────────────────────────────────────────────────

def scrape_cessionpme(existing: dict) -> list:
    """cessionpme.com — marketplace cession PME"""
    new_deals = []
    base = "https://www.cessionpme.com"

    urls_to_try = [
        f"{base}/annonces",
        f"{base}/offres",
        f"{base}/recherche?secteur=agroalimentaire",
    ]

    for url in urls_to_try:
        soup = get_soup(url)
        if not soup:
            time.sleep(2)
            continue

        cards = soup.select("article, .annonce, .offer, .listing-item, .card")

        for card in cards:
            title_el  = card.select_one("h2, h3, h4, .title")
            link_el   = card.select_one("a[href]")
            desc_el   = card.select_one("p, .description")
            region_el = card.select_one(".region, .location, .departement")
            ca_el     = card.select_one(".ca, .chiffre, .revenue")
            date_el   = card.select_one(".date, time")

            if not (title_el or link_el):
                continue

            title  = (title_el or link_el).get_text(strip=True)
            href   = link_el.get("href", "") if link_el else ""
            if href and not href.startswith("http"):
                href = base + href
            desc   = desc_el.get_text(strip=True) if desc_el else ""
            region = region_el.get_text(strip=True) if region_el else ""
            ca_txt = ca_el.get_text(strip=True) if ca_el else ""
            date_pub = date_el.get_text(strip=True) if date_el else ""

            if not title or len(title) < 5:
                continue
            if not matches_sector(f"{title} {desc}"):
                continue
            if not ca_in_range(parse_ca(ca_txt + " " + desc)):
                continue

            did = deal_id(href or title, title)
            if did in existing or did in {d["id"] for d in new_deals}:
                continue

            deal = make_deal(title, href or url, "CessionPME", region, ca_txt, desc, date_pub)
            new_deals.append(deal)
            print(f"    ✚ {title[:65]}")

        time.sleep(2)
        if new_deals:
            break

    return new_deals


# ─── Scraper: MaCessionEntreprise ──────────────────────────────────────────────

def scrape_macession(existing: dict) -> list:
    """macessionentreprise.fr"""
    new_deals = []
    base = "https://www.macessionentreprise.fr"

    urls_to_try = [
        f"{base}/annonces",
        f"{base}/offres",
        f"{base}",
    ]

    for url in urls_to_try:
        soup = get_soup(url)
        if not soup:
            time.sleep(2)
            continue

        cards = soup.select("article, .annonce, .offer, .card, .listing")

        for card in cards:
            title_el  = card.select_one("h2, h3, h4, .title")
            link_el   = card.select_one("a[href]")
            desc_el   = card.select_one("p, .description")
            region_el = card.select_one(".region, .location")
            ca_el     = card.select_one(".ca, .chiffre")
            date_el   = card.select_one(".date, time")

            if not (title_el or link_el):
                continue

            title  = (title_el or link_el).get_text(strip=True)
            href   = link_el.get("href", "") if link_el else ""
            if href and not href.startswith("http"):
                href = base + href
            desc   = desc_el.get_text(strip=True) if desc_el else ""
            region = region_el.get_text(strip=True) if region_el else ""
            ca_txt = ca_el.get_text(strip=True) if ca_el else ""
            date_pub = date_el.get_text(strip=True) if date_el else ""

            if not title or len(title) < 5:
                continue
            if not matches_sector(f"{title} {desc}"):
                continue
            if not ca_in_range(parse_ca(ca_txt + " " + desc)):
                continue

            did = deal_id(href or title, title)
            if did in existing or did in {d["id"] for d in new_deals}:
                continue

            deal = make_deal(title, href or url, "MaCession", region, ca_txt, desc, date_pub)
            new_deals.append(deal)
            print(f"    ✚ {title[:65]}")

        time.sleep(2)
        if new_deals:
            break

    return new_deals


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*65}")
    print(f"  🔍 Acquis Deal Flow Scraper — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Sectors: petfood | nutrition animale | équin | NAC | volailles")
    print(f"  CA range: {CA_MIN//1000}K€ — {CA_MAX//1_000_000}M€")
    print(f"{'='*65}\n")

    existing = load_existing()
    print(f"  Existing deals in database: {len(existing)}\n")

    all_new = []

    scrapers = [
        ("Transentreprise", scrape_transentreprise),
        ("BPI France",      scrape_bpi),
        ("Fusacq",          scrape_fusacq),
        ("CRA",             scrape_cra),
        ("CessionPME",      scrape_cessionpme),
        ("MaCession",       scrape_macession),
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

    # Merge and save
    for deal in all_new:
        existing[deal["id"]] = deal

    save_deals(existing)

    # Print summary
    if all_new:
        print(f"\n🆕 New opportunities today:")
        for d in sorted(all_new, key=lambda x: x["source"]):
            ca_str = d["ca"] if d["ca"] != "NC" else "CA?"
            print(f"  [{d['source']:15s}] {d['title'][:50]:50s} | {d['region']:15s} | {ca_str}")

    return 0 if all_new is not None else 1


if __name__ == "__main__":
    sys.exit(main())
