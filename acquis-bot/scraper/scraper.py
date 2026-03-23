#!/usr/bin/env python3
"""
Acquis Deal Flow Scraper v2.3
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

# ─── Verticales & Target Criteria ─────────────────────────────────────

VERTICALES = {
    "Nutrition animale": {
        "keywords_title": [
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
            "friandise", "snack chien", "snack chat",
        ],
        "keywords_broad": [
            "petfood", "pet food",
            "alimentation animale", "nutrition animale", "aliment animal", "aliments animaux",
            "nourriture animale", "croquettes", "pâtée",
            "ruminant", "bovin", "ovin", "caprin", "volaille", "volailles", "aviculture",
            "provenderie", "fabrication aliments", "aliments du bétail",
            "nac", "nouveaux animaux de compagnie", "reptile", "aquariophilie",
            "équin", "équine", "équitation", "cheval", "chevaux", "hippique",
            "nutrition équine", "aliment cheval",
            "animalerie", "jardinerie animalerie",
            "élevage porcin", "porcin", "porc", "friandise",
            "élevage", "vétérinaire", "aquaculture", "apiculture",
            "bétail", "fourrage", "foin", "insecte", "larve", "ver de farine",
            "jardinerie",
        ],
        "ca_min": 300_000,
        "ca_max": 7_000_000,
        "bpi_configs": [
            ("production", "alimentation animale"),
            ("production", "petfood"),
            ("production", "nutrition animale"),
            ("production", "volailles"),
            ("production", "élevage"),
            ("production", "provenderie"),
            ("production", "équin"),
            ("commerce",   "animalerie"),
            ("commerce",   "alimentation animale"),
        ],
        "bpi_sectors": [],
        "fusacq_kw": [
            "petfood", "alimentation animale", "cheval", "élevage",
            "animalerie", "provenderie", "nutrition animale", "équin",
            "volailles", "friandise",
        ],
        "cra_naf": ["422", "424", "430", "621", "623", "628", "705"],
    },
    "Pièces détachées auto": {
        "keywords_title": [
            "pièces détachées", "pièces auto", "pièces automobiles",
            "équipement automobile", "équipements automobiles",
            "accessoires auto", "accessoires automobile",
            "rechange auto", "pièces de rechange",
            "carrosserie", "pneumatique", "pneu",
            "mécanique auto", "entretien auto",
            "démontage auto", "épaviste",
            "distributeur auto", "grossiste auto", "fournitures auto",
        ],
        "keywords_broad": [
            "pièces détachées", "pièces auto", "pièces automobiles",
            "équipement automobile", "équipements automobiles",
            "accessoires auto", "rechange auto",
            "carrosserie", "pneumatique", "pneu",
            "mécanique auto", "démontage auto",
            "distributeur auto", "grossiste auto", "automobile",
        ],
        "ca_min": 300_000,
        "ca_max": 7_000_000,
        "bpi_configs": [
            ("commerce", "pièces détachées automobile"),
            ("commerce", "pièces auto"),
            ("production", "équipement automobile"),
        ],
        "bpi_sectors": ["24B"],
        "fusacq_kw": [
            "pièces détachées", "pièces auto",
            "équipement automobile", "accessoires auto",
            "rechange auto", "pneumatique", "carrosserie",
        ],
        "cra_naf": ["453", "293", "383"],
    },
}

# Back-compat constants
CA_MIN = 300_000
CA_MAX = 7_000_000

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


def detect_verticale(title: str, desc: str) -> Optional[str]:
    """Returns the first matching verticale name, or None."""
    n_title = normalize(title)
    n_both = normalize(f"{title} {desc}")
    for vname, vert in VERTICALES.items():
        if any(kw in n_title for kw in vert["keywords_title"]):
            return vname
        if any(kw in n_both for kw in vert["keywords_broad"]):
            return vname
    return None


# Back-compat shims
def matches_sector_strict(title: str) -> bool:
    return detect_verticale(title, "") is not None


def matches_sector_broad(title: str, desc: str) -> bool:
    return detect_verticale(title, desc) is not None


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


def make_deal(title, url, source, region="", ca_text="", desc="", date_pub="", verticale="") -> dict:
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
        "verticale": verticale or list(VERTICALES.keys())[0],
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

            # Match verticale: title or description must contain sector keywords
            vert_name = detect_verticale(title, desc)
            if not vert_name:
                continue

            if not ca_in_range(parse_ca(ca_txt + " " + desc)):
                continue

            did = deal_id(href or title, title)
            if did in existing or did in seen_ids:
                continue

            seen_ids.add(did)
            deal = make_deal(title, href or search_url, "Transentreprise",
                             region, ca_txt, desc, date_pub, vert_name)
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

    # Build search configs from all verticales: (section, kw, sector_code, vert_hint)
    search_configs = []
    for vname, vert in VERTICALES.items():
        for section, kw in vert["bpi_configs"]:
            search_configs.append((section, kw, None, vname))
        for code in vert["bpi_sectors"]:
            search_configs.append(("commerce", None, code, vname))

    for section, kw, sector_code, vert_hint in search_configs:
        if sector_code:
            url = f"{base}/commerce?secteur_activite={sector_code}"
            print(f"    🔍 BPI secteur {sector_code} ({vert_hint})")
        else:
            url = f"{base}/{section}?searchText={requests.utils.quote(kw)}"
            print(f"    🔍 BPI {section}: {kw} ({vert_hint})")
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
            vert_name = detect_verticale(title, desc) or vert_hint
            if not ca_in_range(parse_ca(ca_txt + " " + desc)):
                continue

            did = deal_id(href or title, title)
            if did in existing or did in seen_ids:
                continue

            seen_ids.add(did)
            deal = make_deal(title, href or url, "BPI France",
                             region, ca_txt, desc, date_pub, vert_name)
            new_deals.append(deal)
            print(f"    ✚ {title[:65]}")

        time.sleep(2)

    return new_deals



# ─── Scraper: Fusacq ────────────────────────────────────────────────

def scrape_fusacq(existing: dict, session: requests.Session) -> list:
    """
    fusacq.com — Server-side rendered listing pages.
    Card: .card.no_shadow.mb-3  |  Title: .titre_annonce
    CA/date: .nowrap_custom x3  |  Location: text node after .fa-map-marker-alt
    """
    base = "https://www.fusacq.com"
    search_path = "/reprendre-une-entreprise/resultats-annonces-cession-entreprise_fr_"
    new_deals = []
    seen_ids = set()

    keywords = []
    for _vname, _vert in VERTICALES.items():
        for _kw in _vert["fusacq_kw"]:
            if _kw not in keywords:
                keywords.append(_kw)

    for kw in keywords:
        print(f"    🔍 Fusacq: {kw}")
        for page in range(1, 4):
            sep = "?"
            url = base + search_path + sep + "reference_mots_cles=" + requests.utils.quote(kw) + "&page=" + str(page)
            soup = get_soup(session, url)
            if not soup:
                break

            cards = soup.select(".card.no_shadow.mb-3")
            if not cards:
                break

            found_new = False
            for card in cards:
                title_el = card.select_one(".titre_annonce")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)

                link_el = card.select_one("a.btn-bleu-annonce")
                href = link_el.get("href", "") if link_el else ""
                if not href:
                    continue
                if not href.startswith("http"):
                    href = base + href

                nowraps = card.select(".nowrap_custom")
                ca_raw = nowraps[0].get_text(strip=True) if len(nowraps) > 0 else ""
                date_raw = nowraps[1].get_text(strip=True) if len(nowraps) > 1 else ""

                ca_txt = re.sub(r"^CA\s*:\s*", "", ca_raw, flags=re.IGNORECASE).strip()

                date_pub = ""
                dm = re.search(r"(\d{2})/(\d{2})/(\d{4})", date_raw)
                if dm:
                    date_pub = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"

                region = ""
                marker_el = card.select_one(".fa-map-marker-alt")
                if marker_el and marker_el.next_sibling:
                    ns = marker_el.next_sibling
                    if hasattr(ns, "strip"):
                        region = ns.strip()

                if region and any(c in region for c in ["Suisse", "Belgique", "Canada", "Luxembourg"]):
                    continue

                ca_val = parse_ca(ca_txt)
                if not ca_in_range(ca_val):
                    continue

                desc = card.get_text(separator=" ", strip=True)[:400]
                vert_name = detect_verticale(title, desc)
                if not vert_name:
                    continue

                did = deal_id(href, title)
                if did in existing or did in seen_ids:
                    continue

                seen_ids.add(did)
                deal = make_deal(title, href, "Fusacq", region, ca_txt, desc, date_pub, vert_name)
                new_deals.append(deal)
                print(f"    ✚ {title[:65]}")
                found_new = True

            if not found_new and page > 1:
                break
            time.sleep(2)

    return new_deals


# ─── Scraper: CRA ──────────────────────────────────────────────────────────────

def scrape_cra(existing: dict, session: requests.Session) -> list:
    """
    cra.asso.fr ─── Server-side rendered listing pages.
    Card: article[class*='presAF']  |  Title: p.title a
    Region: p.place  |  CA: p.price
    Sector filter via NAF codes (fact) + CA range (fCA 1─4)
    """
    base = "https://www.cra.asso.fr"
    # NAF codes: 422=prod.animale  424=aquaculture  430=ind.alim
    #            621=aliments betail  623=animaux vivants  628=volailles
    #            705=animalerie + aliments animaux compagnie
    fact_codes = list(dict.fromkeys(
        code for vert in VERTICALES.values() for code in vert["cra_naf"]
    ))
    ca_codes = ["1", "2", "3", "4"]
    new_deals = []
    seen_ids = set()

    print(f"    🔍 CRA: {len(fact_codes)} secteurs NAF")

    for page in range(1, 15):
        params = [("page", str(page))]
        for c in fact_codes:
            params.append(("fact", c))
        for ca in ca_codes:
            params.append(("fCA", ca))
        qs = "&".join(f"{k}={v}" for k, v in params)
        url = base + "/liste-entreprises-a-reprendre.aspx?" + qs
        soup = get_soup(session, url)
        if not soup:
            break

        cards = soup.select("article[class*='presAF']")
        if not cards:
            break

        found_any = False
        for card in cards:
            found_any = True

            title_el = card.select_one("p.title a")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            if href and not href.startswith("http"):
                href = base + href

            place_el = card.select_one("p.place")
            region = place_el.get_text(strip=True) if place_el else ""
            if region and any(c in region for c in ["Suisse", "Belgique", "Canada", "Luxembourg"]):
                continue

            price_el = card.select_one("p.price")
            price_txt = price_el.get_text(strip=True) if price_el else ""
            # Match "CA : 950 000 EUR"
            ca_m = re.search(r"CA\s*:\s*([\d\s,\.]+)", price_txt, re.IGNORECASE)
            ca_txt = ca_m.group(1).strip() if ca_m else ""

            ca_val = parse_ca(ca_txt)
            if not ca_in_range(ca_val):
                continue

            desc = card.get_text(separator=" ", strip=True)[:400]
            vert_name = detect_verticale(title, desc)
            if not vert_name:
                continue

            did = deal_id(href or title, title)
            if did in existing or did in seen_ids:
                continue

            seen_ids.add(did)
            deal = make_deal(title, href or url, "CRA", region, ca_txt, desc, "", vert_name)
            new_deals.append(deal)
            print(f"    ✚ {title[:65]}")

        if not found_any:
            break
        time.sleep(2)

    return new_deals

# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    print(f"\n{'='*65}")
    print(f"  🔍 Acquis Deal Flow Scraper v2.3 — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Verticales: {', '.join(VERTICALES.keys())}")
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
        ("CRA",             lambda e: scrape_cra(e, session)),
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
