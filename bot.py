from __future__ import annotations

import os
import argparse
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "seen.json"
CONFIG_FILE = BASE_DIR / "config.yaml"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 UtrechtHuurBot/1.0"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


@dataclass(frozen=True)
class Listing:
    source: str
    title: str
    url: str
    city: Optional[str] = None
    rent: Optional[int] = None
    area: Optional[int] = None
    rooms: Optional[int] = None

    @property
    def uid(self) -> str:
        raw = f"{self.source}|{self.url}|{self.title}|{self.rent}|{self.area}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise SystemExit("config.yaml niet gevonden. Kopieer config.example.yaml naar config.yaml en vul je gegevens in.")
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if token:
            config["telegram"]["token"] = token
        if chat_id:
            config["telegram"]["chat_id"] = chat_id

    return config

def load_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception:
        logging.warning("seen.json kon niet gelezen worden; start met lege lijst.")
        return set()


def save_seen(seen: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")




def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_euro(text: str) -> Optional[int]:
    # Matches € 1.695, €1695, 1.695,- /mnd, 2000 per maand
    patterns = [
        r"€\s*([0-9]{1,3}(?:[\.,][0-9]{3})*|[0-9]{4,5})",
        r"([0-9]{1,3}(?:[\.,][0-9]{3})*|[0-9]{4,5})\s*,?-?\s*(?:/mnd|per maand|p/m)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            num = re.sub(r"[^0-9]", "", m.group(1))
            if num:
                value = int(num)
                if 100 <= value <= 10000:
                    return value
    return None


def parse_area(text: str) -> Optional[int]:
    m = re.search(r"([0-9]{2,4})\s*(?:m²|m2|m\^2|m\s*2)", text, re.I)
    return int(m.group(1)) if m else None


def parse_rooms(text: str) -> Optional[int]:
    m = re.search(r"([0-9]+)\s*(?:kamers?|slaapkamers?)", text, re.I)
    return int(m.group(1)) if m else None


def likely_listing_blocks(soup: BeautifulSoup) -> list:
    selectors = [
        "article", "li", ".object", ".listing", ".property", ".search-result", ".result", ".card", ".woning", ".aanbod-item"
    ]
    blocks = []
    for sel in selectors:
        for el in soup.select(sel):
            txt = clean_text(el.get_text(" "))
            if len(txt) > 40 and ("€" in txt or "m²" in txt or "m2" in txt):
                blocks.append(el)
    # fallback: relevant anchors
    for a in soup.find_all("a", href=True):
        txt = clean_text(a.get_text(" "))
        parent_txt = clean_text(a.parent.get_text(" ") if a.parent else txt)
        if len(parent_txt) > 40 and ("€" in parent_txt or "m²" in parent_txt or "m2" in parent_txt):
            blocks.append(a.parent)
    # de-dupe by object id
    seen_ids = set()
    unique = []
    for b in blocks:
        if id(b) not in seen_ids:
            unique.append(b)
            seen_ids.add(id(b))
    return unique




def dedupe_listings(listings: Iterable[Listing]) -> list[Listing]:
    out = []
    seen = set()
    for l in listings:
        key = l.url.split("?")[0].rstrip("/")
        if key not in seen:
            out.append(l)
            seen.add(key)
    return out


def scrape_generic(source: str, urls: list[str]) -> list[Listing]:
    all_items: list[Listing] = []
    for url in urls:
        try:
            html = fetch(url)
            soup = BeautifulSoup(html, "lxml")
            all_items.extend(extract_from_blocks(source, url, soup))
        except Exception as e:
            logging.warning("%s mislukt voor %s: %s", source, url, e)
    return dedupe_listings(all_items)

def fetch(url: str) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    return r.text


def extract_jsonld_listings(source: str, base_url: str, soup: BeautifulSoup) -> list[Listing]:
    listings = []

    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        items = data if isinstance(data, list) else [data]

        for item in items:
            if not isinstance(item, dict):
                continue

            graph = item.get("@graph")
            if isinstance(graph, list):
                items.extend(graph)

            name = item.get("name") or item.get("headline")
            url = item.get("url")
            address = item.get("address", {})

            text = json.dumps(item, ensure_ascii=False)
            rent = parse_euro(text)
            area = parse_area(text)

            city = None
            if isinstance(address, dict):
                city = address.get("addressLocality")

            if name and url:
                listings.append(
                    Listing(
                        source=source,
                        title=clean_text(str(name))[:140],
                        url=urljoin(base_url, str(url)),
                        city=city,
                        rent=rent,
                        area=area,
                    )
                )

    return dedupe_listings(listings)


def extract_from_blocks(source: str, base_url: str, soup: BeautifulSoup) -> list[Listing]:
    listings: list[Listing] = []

    listings.extend(extract_jsonld_listings(source, base_url, soup))

    for block in likely_listing_blocks(soup):
        text = clean_text(block.get_text(" "))
        rent = parse_euro(text)
        area = parse_area(text)
        rooms = parse_rooms(text)

        links = block.find_all("a", href=True) if hasattr(block, "find_all") else []
        if not links:
            continue

        best_link = links[0]
        for a in links:
            href = a.get("href", "")
            if any(word in href.lower() for word in ["woning", "huur", "aanbod", "appartement", "object"]):
                best_link = a
                break

        url = urljoin(base_url, best_link["href"])
        title = clean_text(best_link.get_text(" "))

        heading = block.find(["h1", "h2", "h3", "h4"]) if hasattr(block, "find") else None
        if heading:
            heading_text = clean_text(heading.get_text(" "))
            if len(heading_text) > len(title):
                title = heading_text

        if not title or len(title) < 4:
            title = text[:90]

        if not rent and not area:
            continue

        city = "Utrecht" if "utrecht" in f"{text} {url}".lower() else None

        listings.append(
            Listing(
                source=source,
                title=title[:140],
                url=url,
                city=city,
                rent=rent,
                area=area,
                rooms=rooms,
            )
        )

    return dedupe_listings(listings)


def scrape_all(config: dict) -> list[Listing]:
    results: list[Listing] = []
    sources = config.get("sources", {})

    for source, cfg in sources.items():
        if not cfg or not cfg.get("enabled", False):
            continue

        urls = cfg.get("urls", [])
        logging.info("Check %s (%d url's)", source, len(urls))
        results.extend(scrape_generic(source.upper(), urls))

    return dedupe_listings(results)

def matches_filters(l: Listing, filters: dict) -> bool:
    city = (filters.get("city") or "").lower()
    max_rent = int(filters.get("max_rent", 999999))
    min_area = int(filters.get("min_area", 0))

    haystack = f"{l.title} {l.city or ''} {l.url}".lower()
    if city and city not in haystack:
        return False
    if l.rent is None or l.rent > max_rent:
        return False
    if l.area is None or l.area < min_area:
        return False
    return True


def format_message(l: Listing) -> str:
    parts = [
        "🏠 Nieuwe huurwoning gevonden!",
        f"\nBron: {l.source}",
        f"Titel: {l.title}",
    ]
    if l.rent is not None:
        parts.append(f"Prijs: €{l.rent:,} p/m".replace(",", "."))
    if l.area is not None:
        parts.append(f"Oppervlakte: {l.area} m²")
    if l.rooms is not None:
        parts.append(f"Kamers/slaapkamers: {l.rooms}")
    parts.append(f"\nLink: {l.url}")
    return "\n".join(parts)


def send_telegram(config: dict, text: str) -> None:
    token = str(config["telegram"]["token"]).strip()
    chat_id = str(config["telegram"]["chat_id"]).strip()
    if not token or "VUL_HIER" in token or not chat_id or "VUL_HIER" in chat_id:
        raise RuntimeError("Telegram token/chat_id ontbreken in config.yaml")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False}, timeout=20)
    r.raise_for_status()


def check_once(config: dict, seen: set[str], first_run: bool = False) -> tuple[int, int]:
    listings = scrape_all(config)
    matches = [l for l in listings if matches_filters(l, config.get("filters", {}))]
    logging.info("%d listings gevonden, %d voldoen aan filters", len(listings), len(matches))

    sent = 0
    notify_existing = bool(config.get("notify_existing_on_first_run", True))
    for l in matches:
        if l.uid in seen:
            continue
        seen.add(l.uid)
        if first_run and not notify_existing:
            logging.info("Bestaande match gemarkeerd als gezien: %s", l.title)
            continue
        logging.info("Nieuwe match: %s | €%s | %sm2", l.title, l.rent, l.area)
        send_telegram(config, format_message(l))
        sent += 1
    if bool(config.get("send_summary_when_no_new", False)) and sent == 0:
        send_telegram(
            config,
            f"✅ Bot actief\n"
            f"{len(listings)} listings gevonden\n"
            f"{len(matches)} voldoen aan filters\n"
            f"{sent} nieuwe meldingen verstuurd"
        )
    
    save_seen(seen)
    return len(matches), sent

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="voer één controle uit en stop")
    parser.add_argument("--test-telegram", action="store_true", help="stuur een testmelding")
    args = parser.parse_args()

    config = load_config()

    if args.test_telegram:
        send_telegram(config, "✅ Testmelding van je Utrecht Huur Bot. Telegram werkt!")
        print("Testmelding verstuurd.")
        return

    seen = load_seen()
    first = not STATE_FILE.exists()

    if args.once:
        matches, sent = check_once(config, seen, first_run=first)
        print(f"Klaar. Matches: {matches}. Meldingen verstuurd: {sent}.")
        return

    interval = int(config.get("check_interval_seconds", 60))
    logging.info("Bot gestart. Controle elke %d seconden.", interval)
    while True:
        try:
            matches, sent = check_once(config, seen, first_run=first)
            first = False
            logging.info("Ronde klaar. Matches: %d. Verstuurd: %d.", matches, sent)
        except Exception as e:
            logging.exception("Controle mislukt: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
