#!/usr/bin/env python3
"""
Acquis Deal Flow Scraper v3.0 — REFONTE 30/04/2026
Scrapes French M&A platforms 2x/day for Lughanor's new thesis :
sous-traitance industrielle (NAF 22-28) + industrie/mécanique B2B.

Geographic priority :
  P1 = Alsace (départements 67 + 68)
  P2 = 35 (Ille-et-Vilaine), 13 (Bouches-du-Rhône), 83 (Var)
  Out = anything else (still scraped, but flagged "out_of_scope")

Sources (confirmed):
- Transentreprise.com           — POST search (équivalent Actify CCI)
- BPI France Transmission       — alias "Actify" / Reprise & Transmission
- Fusacq                        — Marketplace Fusions & Acquisitions
- CRA / CessionPME              — Conseil National des Repreneurs (CRA) + listings PME
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

# ─── Verticales & Target Criteria — REFONTE 30/04/2026 ──────────────────────

VERTICALES = {
    "Sous-traitance industrielle": {
        "keywords_title": [
            # Plasturgie / caoutchouc (NAF 22)
            "plasturgie", "plastique technique", "injection plastique", "injection",
            "extrusion", "thermoformage", "soufflage", "rotomoulage",
            "caoutchouc", "élastomère", "elastomère", "elastomere",
            # Métallurgie / chaudronnerie / mécanique (NAF 24-28)
            "métallurgie", "metallurgie", "chaudronnerie", "chaudronnier",
            "tôlerie", "tolerie", "découpe métal", "decoupe metal", "emboutissage",
            "soudure", "soudage", "usinage", "décolletage", "decolletage",
            "mécanique de précision", "mecanique de precision",
            "mécanique générale", "mecanique generale",
            "fonderie", "forge", "outillage", "moule", "moulage",
            "traitement de surface", "anodisation", "galvanisation",
            "machine spéciale", "machine speciale", "machines speciales",
            # Sous-traitance industrielle
            "sous-traitance industrielle", "sous-traitant industriel",
            "transformation métallique", "assemblage industriel",
        ],
        "keywords_broad": [
            # SCRAPER REFONTE 30/04/2026 — broad list TIGHTENED, removed generic terms
            # like "industrie", "industriel", "atelier", "fabrication" alone which were
            # matching butcher shops, garages etc.
            "plasturgie", "caoutchouc", "élastomère", "elastomere",
            "métallurgie", "metallurgie", "chaudronnerie", "tôlerie", "tolerie",
            "mécanique de précision", "mecanique de precision",
            "usinage", "décolletage", "decolletage",
            "fonderie", "outillage industriel",
            "sous-traitance", "sous-traitant industriel",
            "transformation métallique",
            "cnc", "tournage industriel", "fraisage industriel",
        ],
        "ca_min": 3_000_000,
        "ca_max": 10_000_000,
        "bpi_configs": [
            ("production", "sous-traitance industrielle"),
            ("production", "plasturgie"),
            ("production", "chaudronnerie"),
            ("production", "mécanique de précision"),
            ("production", "métallurgie"),
            ("production", "usinage"),
            ("production", "découpe métal"),
            ("production", "fonderie"),
        ],
        "bpi_sectors": ["22", "24", "25", "28"],  # NAF 22 plastique, 24 métallurgie, 25 fabrication métallique, 28 machines
        "fusacq_kw": [
            "sous-traitance", "plasturgie", "chaudronnerie", "mécanique",
            "usinage", "métallurgie", "fonderie", "tôlerie", "décolletage",
        ],
        "cra_naf": ["22", "24", "25", "28"],
    },
    "Mécanique B2B / Industrie": {
        "keywords_title": [
            "équipementier", "equipementier", "biens d'équipement",
            "équipement industriel", "equipement industriel",
            "machine outil", "machine-outil",
            "robotique industrielle", "automatisme industriel",
            "convoyeur", "convoyage", "manutention industrielle",
            "ligne de production", "ligne d'assemblage",
            "intégrateur industriel", "integrateur industriel",
        ],
        "keywords_broad": [
            # TIGHTENED — removed generic "industriel"/"équipement"/"machines"/"b2b"
            # that were polluting results. Now only specific compound keywords.
            "machine outil", "machine-outil",
            "robotique industrielle", "automatisme industriel",
            "intégrateur industriel", "integrateur industriel",
            "ligne de production", "ligne d'assemblage",
        ],
        "ca_min": 3_000_000,
        "ca_max": 10_000_000,
        "bpi_configs": [
            ("production", "équipement industriel"),
            ("production", "machine outil"),
            ("production", "robotique industrielle"),
        ],
        "bpi_sectors": ["28", "29"],
        "fusacq_kw": [
            "équipementier", "machine outil", "robotique", "automatisme",
            "convoyeur", "intégrateur", "équipement industriel",
        ],
        "cra_naf": ["28", "29"],
    },
    "Nutrition animale (P3 — pipeline secondaire)": {
        # Conservé en P3 — pipeline secondaire, en pause focus principal
        "keywords_title": [
            "petfood", "alimentation animale", "nutrition animale",
            "croquettes", "provenderie", "aliment animal",
        ],
        "keywords_broad": [
            "petfood", "alimentation animale", "nutrition animale",
            "provenderie",
        ],
        "ca_min": 3_000_000,
        "ca_max": 10_000_000,
        "bpi_configs": [
            ("production", "alimentation animale"),
        ],
        "bpi_sectors": [],
        "fusacq_kw": ["petfood", "alimentation animale"],
        "cra_naf": ["108"],
    },
}

# Back-compat constants — aligned with new thesis
CA_MIN = 3_000_000
CA_MAX = 10_000_000

# Geographic priority (per user request 30/04/2026)
REGION_P1 = {"67", "68", "alsace", "bas-rhin", "haut-rhin", "strasbourg", "mulhouse", "colmar"}
REGION_P2 = {"35", "13", "83", "ille-et-vilaine", "rennes", "bouches-du-rhône",
             "marseille", "aix-en-provence", "var", "toulon"}

def detect_region_priority(text: str) -> str:
    """Returns 'P1' | 'P2' | 'OUT' based on region keywords / department codes."""
    n = (text or "").lower()
    # Match on word boundaries for dept codes
    for kw in REGION_P1:
        if re.search(r'\b' + re.escape(kw) + r'\b', n):
            return "P1"
    for kw in REGION_P2:
        if re.search(r'\b' + re.escape(kw) + r'\b', n):
            return "P2"
    return "OUT"

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
    """Returns the first matching verticale name, or None.
    REFONTE 30/04/2026: strict matching on title only — broad keywords
    were causing false positives like 'boucherie' classified as sous-traitance industrielle.
    """
    n_title = normalize(title)
    for vname, vert in VERTICALES.items():
        if any(kw in n_title for kw in vert["keywords_title"]):
            return vname
    # Fallback: broad match BOTH in title AND description (require 1 match in each side)
    # This catches edge cases without exploding false positives
    n_desc = normalize(desc)
    for vname, vert in VERTICALES.items():
        broad_in_title = any(kw in n_title for kw in vert["keywords_broad"])
        broad_in_desc = any(kw in n_desc for kw in vert["keywords_broad"])
        if broad_in_title and broad_in_desc:
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
    """Save deals to JSON. REFONTE 30/04/2026:
    - Drop deals classified as legacy verticales (Pièces auto, Mapping, old Nutrition)
    - Drop deals with region_priority='OUT' (off-thesis geo)
    - Keep only last 90 days
    """
    LEGACY_VERTICALES = {"Pièces détachées auto", "Mapping concurrentiel", "Nutrition animale"}
    deals_list = list(deals_by_id.values())
    # Filter 1: drop legacy verticales (force re-scraping under new ones)
    deals_list = [d for d in deals_list
                  if (d.get("verticale") or "") not in LEGACY_VERTICALES]
    # Filter 2: drop off-thesis geographies
    deals_list = [d for d in deals_list
                  if d.get("region_priority", "P1") in ("P1", "P2")]
    # Filter 3: drop too-old entries
    deals_list.sort(key=lambda d: (d.get("date_scraped", ""), d.get("source", "")), reverse=True)
    cutoff = (datetime.utcnow() - timedelta(days=90)).date().isoformat()
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
    print(f"\n✓ deals.json updated — {len(deals_list)} on-thesis deals (legacy + OUT dropped)")


def make_deal(title, url, source, region="", ca_text="", desc="", date_pub="", verticale="") -> dict:
    ca_val = parse_ca(ca_text + " " + desc)
    region_prio = detect_region_priority(f"{region} {title} {desc}")
    return {
        "id": deal_id(url, title),
        "title": title.strip(),
        "url": url.strip(),
        "source": source,
        "region": region.strip(),
        "region_priority": region_prio,  # P1 / P2 / OUT (refonte 30/04/2026)
        "ca": ca_text.strip() or "NC",
        "ca_value": ca_val,
        "description": desc[:400].strip() if desc else "",
        "date_pub": date_pub.strip(),
        "date_scraped": datetime.utcnow().date().isoformat(),
        "date_scraped_iso": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
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

# ─── Scraper: CessionPME (stub — to wire up when site structure confirmed) ─────

def scrape_cessionpme(existing: dict, session: requests.Session) -> list:
    """
    CessionPME.com — Stub scraper. Site has anti-bot protection (Cloudflare).
    To be wired up properly with sitemap or RSS feed when accessible.
    """
    print("    (stub — CessionPME scraping pending integration)")
    return []


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    print(f"\n{'='*65}")
    print(f"  🔍 Acquis Deal Flow Scraper v3.0 — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Thesis: Lughanor — Sous-traitance industrielle + Mécanique B2B")
    print(f"  Verticales: {', '.join(VERTICALES.keys())}")
    print(f"  CA range: {CA_MIN//1_000_000}M€ — {CA_MAX//1_000_000}M€")
    print(f"  Geo P1: Alsace 67/68 | P2: 35/13/83 | other: tagged out_of_scope")
    print(f"  Deals file: {DEALS_FILE}")
    print(f"{'='*65}\n")

    existing = load_existing()
    print(f"  Existing deals in database: {len(existing)}\n")

    session = requests.Session()
    session.headers.update(HEADERS)

    all_new = []

    scrapers = [
        ("Transentreprise (Actify CCI)", lambda e: scrape_transentreprise(e, session)),
        ("BPI France (Reprise & Transmission)", lambda e: scrape_bpi(e, session)),
        ("Fusacq",              lambda e: scrape_fusacq(e, session)),
        ("CRA",                 lambda e: scrape_cra(e, session)),
        ("CessionPME",          lambda e: scrape_cessionpme(e, session)),
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

    # Stats by region priority
    prio_counts = {"P1": 0, "P2": 0, "OUT": 0}
    for deal in all_new:
        existing[deal["id"]] = deal
        prio_counts[deal.get("region_priority", "OUT")] += 1
    print(f"  Region priority — P1 (Alsace): {prio_counts['P1']} | "
          f"P2 (35/13/83): {prio_counts['P2']} | OUT: {prio_counts['OUT']}")

    save_deals(existing)

    if all_new:
        print(f"\n🆕 New opportunities today:")
        for d in sorted(all_new, key=lambda x: (x.get("region_priority", "Z"), x["source"])):
            ca_str = d["ca"] if d["ca"] != "NC" else "CA?"
            prio = d.get("region_priority", "?")
            print(f"  [{prio}] [{d['source'][:18]:18s}] {d['title'][:45]:45s} | "
                  f"{d['region'][:15]:15s} | {ca_str}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
