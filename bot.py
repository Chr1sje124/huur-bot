from __future__ import annotations

import os
import argparse
import hashlib
import json
import logging
import re
import time
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
import yaml
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "seen.json"
CONFIG_FILE = BASE_DIR / "config.yaml"
STATE_VERSION_MARKER = "__stable_url_ids_v2__"
STATE_VERSION = 2
DEBUG_DIR = BASE_DIR / "debug"
UTC = timezone.utc

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


class ConfigurationError(RuntimeError):
    pass


class StateError(RuntimeError):
    pass


class BlockedPageError(RuntimeError):
    pass


class InvalidHtmlResponseError(RuntimeError):
    pass


class ScrapeHealthError(RuntimeError):
    pass


@dataclass(frozen=True)
class Listing:
    source: str
    title: str
    url: str
    city: Optional[str] = None
    rent: Optional[int] = None
    area: Optional[int] = None
    rooms: Optional[int] = None
    availability: str = "unknown"
    available_from: Optional[str] = None
    postcode: Optional[str] = None

    @property
    def uid(self) -> str:
        # Price and area can change while the advert is still the same home.
        # Using them here caused duplicate Telegram notifications.
        raw = f"{self.source.lower()}|{canonical_url(self.url)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass
class SourceResult:
    source: str
    listings: list[Listing] = field(default_factory=list)
    urls_attempted: int = 0
    requests_ok: int = 0
    requests_failed: int = 0
    raw_candidates: int = 0
    unique_listings: int = 0
    with_rent: int = 0
    with_area: int = 0
    complete_listings: int = 0
    matches: int = 0
    possible_matches: int = 0
    available: int = 0
    unavailable: int = 0
    detail_requests: int = 0
    blocked: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.blocked:
            return "blocked"
        if self.urls_attempted and self.requests_ok == 0:
            return "failed"
        if self.raw_candidates and self.complete_listings / self.raw_candidates < 0.1:
            return "parsing_warning"
        if self.requests_failed:
            return "partial"
        return "healthy"

    def finalize(self, filters: Optional[dict] = None) -> None:
        self.listings = dedupe_listings(self.listings)
        self.unique_listings = len(self.listings)
        self.with_rent = sum(item.rent is not None for item in self.listings)
        self.with_area = sum(item.area is not None for item in self.listings)
        self.complete_listings = sum(item.rent is not None and item.area is not None for item in self.listings)
        self.available = sum(item.availability == "available" for item in self.listings)
        self.unavailable = sum(item.availability in {"reserved", "rented"} for item in self.listings)
        if filters is not None:
            classes = [classify_listing(item, filters) for item in self.listings]
            self.matches = classes.count("match")
            self.possible_matches = classes.count("possible_match")


class SeenState(set[str]):
    """Set-compatible view that carries the versioned state document."""
    def __init__(self, values: Iterable[str] = (), state: Optional[dict] = None):
        super().__init__(values)
        self.state = state or {"version": STATE_VERSION, "listings": {}, "sources": {}}


TRACKING_PARAMS = {"gclid", "fbclid", "ref", "source"}

# Actual advert paths observed on each provider. This prevents navigation,
# projects and marketing cards from being mistaken for available homes.
SOURCE_LINK_PATTERNS = {
    "funda": (r"/detail/huur/",),
    "rebo": (r"/nl/aanbod/",),
    "mvgm": (r"/aanbod/(?:huurwoning|woning|appartement|huis)/[^?#]+",),
    "vesteda": (r"/nl/huurwoning[^?#]*/[^/?#]+-\d+/?$",),
    "vbt": (r"/woning/",),
    "nmg": (r"/woning/",),
    "holland2stay": (r"/woningaanbod/[^/?#]+\.html",),
    "heimstaden": (r"/nl/huurwoningen/[^/?#]*(?:m²|m2|[0-9a-f]{8}-)[^/?#]*",),
    "pararius": (r"/(?:appartement|huis|kamer)-te-huur/",),
    "woningnet": (r"/(?:aanbod|woningaanbod|advertentie)/[^?#]+",),
    "woonin": (r"/(?:woning|woningaanbod|aanbod)/[^?#]+",),
    "portaal": (r"/(?:woning|aanbod)/[^?#]+",),
    "boex": (r"/(?:woning|aanbod)/[^?#]+",),
    "mitros": (r"/(?:woning|aanbod)/[^?#]+",),
    "nieuwbouw": (r"/(?:woning|appartement|huurwoning|aanbod)/[^?#]+",),
}

UTRECHT_POSTCODE_PREFIXES = tuple(str(number) for number in range(3500, 3586))
DEFAULT_UTRECHT_AREAS = (
    "utrecht", "leidsche rijn", "vleuten", "de meern", "haarzuilens", "lunetten",
    "overvecht", "kanaleneiland", "zuilen", "tuindorp", "wittevrouwen", "lombok",
    "oog in al", "terwijde", "vleuterweide", "papendorp", "hoograven",
)


def canonical_url(url: str) -> str:
    """Return a stable advert URL, without fragments and tracking parameters."""
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def is_valid_listing_url(source: str, url: str, base_url: str = "") -> bool:
    canonical = canonical_url(url)
    if not urlsplit(canonical).netloc or urlsplit(canonical).path == "/":
        return False
    if base_url and canonical == canonical_url(base_url):
        return False
    patterns = SOURCE_LINK_PATTERNS.get(source.lower(), ())
    if patterns and not any(re.search(pattern, canonical, re.I) for pattern in patterns):
        return False
    path = urlsplit(canonical).path.lower()
    if source.lower() == "mvgm" and path.rstrip("/") in {"/aanbod/utrecht", "/aanbod/zorgwoningen"}:
        return False
    return True


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise SystemExit("config.yaml niet gevonden. Kopieer config.example.yaml naar config.yaml en vul je gegevens in.")
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

        if not isinstance(config, dict):
            raise SystemExit("config.yaml heeft geen geldige structuur.")
        config.setdefault("telegram", {})

        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if token:
            config["telegram"]["token"] = token
        if chat_id:
            config["telegram"]["chat_id"] = chat_id

    return validate_config(config)


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ConfigurationError("config.yaml moet een mapping zijn")
    filters = config.setdefault("filters", {})
    filters.setdefault("allow_missing_rent", False)
    filters.setdefault("allow_missing_area", False)
    for key in ("allow_missing_rent", "allow_missing_area"):
        if not isinstance(filters[key], bool):
            raise ConfigurationError(f"filters.{key} moet true of false zijn")
    for key in ("location_terms", "postcode_prefixes"):
        if key in filters and (not isinstance(filters[key], list) or not all(isinstance(value, (str, int)) for value in filters[key])):
            raise ConfigurationError(f"filters.{key} moet een lijst zijn")
    if isinstance(filters.get("max_rent"), bool) or int(filters.get("max_rent", 0)) <= 0:
        raise ConfigurationError("filters.max_rent moet positief zijn")
    if isinstance(filters.get("min_area"), bool) or int(filters.get("min_area", 0)) < 0:
        raise ConfigurationError("filters.min_area mag niet negatief zijn")
    interval = config.get("check_interval_seconds", 60)
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise ConfigurationError("check_interval_seconds moet een positief geheel getal zijn")
    active = 0
    for name, source in config.get("sources", {}).items():
        if not isinstance(source, dict) or not isinstance(source.get("enabled", False), bool):
            raise ConfigurationError(f"sources.{name} is ongeldig")
        if source.get("enabled"):
            active += 1
            urls = source.get("urls")
            if not isinstance(urls, list) or not urls or not all(isinstance(url, str) and url.startswith(("http://", "https://")) for url in urls):
                raise ConfigurationError(f"actieve bron {name} mist geldige urls")
        for key in ("max_pages", "max_detail_requests"):
            if key in source and (isinstance(source[key], bool) or not isinstance(source[key], int) or source[key] < (1 if key == "max_pages" else 0)):
                raise ConfigurationError(f"sources.{name}.{key} is ongeldig")
        if "enrich_details" in source and not isinstance(source["enrich_details"], bool):
            raise ConfigurationError(f"sources.{name}.enrich_details moet boolean zijn")
    if config.get("sources") and not active:
        raise ConfigurationError("minimaal één bron moet actief zijn")
    notifications = config.setdefault("notifications", {})
    notifications.setdefault("send_no_new_summary", bool(config.get("send_summary_when_no_new", False)))
    notifications.setdefault("notify_on_source_failure", True)
    notifications.setdefault("notify_on_recovery", True)
    notifications.setdefault("source_failure_repeat_hours", 12)
    notifications.setdefault("notify_on_price_drop", True)
    notifications.setdefault("notify_on_price_increase", False)
    notifications.setdefault("send_possible_matches", True)
    config.setdefault("state", {}).setdefault("stale_after_days", 60)
    return config

def load_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            backup = STATE_FILE.with_suffix(".json.v1.bak")
            if not backup.exists():
                shutil.copy2(STATE_FILE, backup)
            state = {"version": STATE_VERSION, "listings": {}, "sources": {}, "legacy_seen_ids": sorted(set(raw))}
            return SeenState(raw, state)
        if isinstance(raw, dict):
            raw.setdefault("version", STATE_VERSION)
            raw.setdefault("listings", {})
            raw.setdefault("sources", {})
            values = set(raw.get("legacy_seen_ids", [])) | set(raw.get("listings", {}))
            return SeenState(values, raw)
        raise StateError("seen.json heeft geen ondersteund formaat")
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"seen.json kon niet betrouwbaar gelezen worden: {exc}") from exc


def save_seen(seen: set[str]) -> None:
    # Atomic replace prevents a truncated JSON file when a process is stopped.
    state = getattr(seen, "state", {"version": STATE_VERSION, "listings": {}, "sources": {}})
    state["version"] = STATE_VERSION
    state.setdefault("listings", {})
    state.setdefault("sources", {})
    state["legacy_seen_ids"] = sorted(value for value in seen if value not in state["listings"])
    data = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    json.loads(data)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=BASE_DIR, delete=False) as f:
        f.write(data)
        temporary = Path(f.name)
    temporary.replace(STATE_FILE)




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


def parse_rent(text: str) -> Optional[int]:
    """Parse monthly rent while avoiding common ancillary amounts."""
    cleaned = clean_text(text).replace("\u00a0", " ")
    patterns = (
        r"(?:€|EUR)\s*([0-9][0-9., ]{2,8})",
        r"([0-9][0-9., ]{2,8})\s*(?:,-)?\s*(?:/\s*(?:mnd|maand)|p/?m|per maand)",
        r"([0-9][0-9., ]{2,8})\s*(?:euro|eur)\s*(?:/\s*(?:mnd|maand)|p/?m|per maand)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, cleaned, re.I):
            context = cleaned[max(0, match.start() - 25):match.end() + 30].lower()
            if any(word in context for word in ("servicekosten", "borg", "waarborg", "koopprijs", "oude prijs")):
                continue
            digits = re.sub(r"\D", "", match.group(1))
            if digits and 100 <= int(digits) <= 25000:
                return int(digits)
    if any(word in cleaned.lower() for word in ("servicekosten", "borg", "waarborg", "koopprijs", "oude prijs")):
        return None
    return parse_euro(cleaned)


def parse_area(text: str) -> Optional[int]:
    normalized = text.replace("m²", "m2")
    decimal = re.search(r"([0-9]{1,4}(?:[.,][0-9])?)\s*m2", normalized, re.I)
    if decimal:
        return round(float(decimal.group(1).replace(",", ".")))
    m = re.search(r"([0-9]{2,4})\s*(?:m²|m2|m\^2|m\s*2)", text, re.I)
    return int(m.group(1)) if m else None


def parse_rooms(text: str) -> Optional[int]:
    m = re.search(r"([0-9]+)\s*(?:kamers?|slaapkamers?)", text, re.I)
    return int(m.group(1)) if m else None


def parse_postcode(text: str) -> Optional[str]:
    match = re.search(r"\b([1-9][0-9]{3})\s*([A-Z]{2})\b", text, re.I)
    return f"{match.group(1)} {match.group(2).upper()}" if match else None


def parse_availability(text: str) -> tuple[str, Optional[str]]:
    lowered = clean_text(text).lower()
    if re.search(r"\b(?:verhuurd|niet meer beschikbaar|niet beschikbaar)\b", lowered):
        return "rented", None
    if re.search(r"\b(?:gereserveerd|onder optie|in optie)\b", lowered):
        return "reserved", None
    date_match = re.search(r"(?:beschikbaar|oplevering)(?:\s+per|\s+vanaf|:)?\s*([0-3]?\d[-/.][01]?\d[-/.](?:20)?\d{2})", lowered)
    if date_match:
        return "available", date_match.group(1)
    if re.search(r"\b(?:beschikbaar|per direct|direct beschikbaar|te huur)\b", lowered):
        return "available", None
    return "unknown", None


def is_utrecht_location(listing: Listing, filters: dict) -> bool:
    haystack = clean_text(f"{listing.title} {listing.city or ''} {listing.postcode or ''} {listing.url}").lower()
    areas = tuple(clean_text(str(value)).lower() for value in filters.get("location_terms", DEFAULT_UTRECHT_AREAS))
    if any(area and area in haystack for area in areas):
        return True
    postcode = listing.postcode or parse_postcode(haystack)
    return bool(postcode and postcode[:4] in set(filters.get("postcode_prefixes", UTRECHT_POSTCODE_PREFIXES)))


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


def source_listing_blocks(source: str, soup: BeautifulSoup) -> list:
    """Find cards containing links that are known to be real adverts."""
    patterns = SOURCE_LINK_PATTERNS.get(source.lower(), ())
    if not patterns:
        return likely_listing_blocks(soup)
    blocks = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if not any(re.search(pattern, href, re.I) for pattern in patterns):
            continue
        block = anchor
        for parent in anchor.parents:
            if parent.name in {"article", "li"} or any(
                token in " ".join(parent.get("class", [])).lower()
                for token in ("card", "result", "object", "property", "woning", "offer")
            ):
                block = parent
                break
        marker = id(block)
        if marker not in seen:
            blocks.append(block)
            seen.add(marker)
    return blocks




def merge_listings(current: Listing, incoming: Listing) -> Listing:
    availability = current.availability
    if availability == "unknown" or incoming.availability in {"reserved", "rented"}:
        availability = incoming.availability
    return Listing(
        source=current.source or incoming.source,
        title=incoming.title if len(incoming.title) > len(current.title) else current.title,
        url=canonical_url(current.url or incoming.url),
        city=current.city or incoming.city,
        rent=current.rent if current.rent is not None else incoming.rent,
        area=current.area if current.area is not None else incoming.area,
        rooms=current.rooms if current.rooms is not None else incoming.rooms,
        availability=availability,
        available_from=current.available_from or incoming.available_from,
        postcode=current.postcode or incoming.postcode,
    )


def dedupe_listings(listings: Iterable[Listing]) -> list[Listing]:
    """Combineer dubbele advertenties en behoud de meest complete gegevens."""
    merged: dict[str, Listing] = {}
    order: list[str] = []

    for listing in listings:
        key = canonical_url(listing.url)
        current = merged.get(key)

        if current is None:
            merged[key] = listing
            order.append(key)
            continue

        merged[key] = merge_listings(current, listing)

    return [merged[key] for key in order]


BLOCK_SIGNALS = ("captcha", "access denied", "verify you are human", "cloudflare", "toegang geweigerd", "challenge-platform")


def validate_html_response(html: str, *, source: str, url: str) -> None:
    soup = BeautifulSoup(html, "lxml")
    visible = clean_text(soup.get_text(" ")).lower()
    title = clean_text(soup.title.get_text(" ") if soup.title else "").lower()
    hard_signals = (
        "verify you are human", "verifiëren dat onze bezoekers echte mensen zijn",
        "verifiã«ren dat onze bezoekers echte mensen zijn", "je bent bijna op de pagina die je zoekt",
        "access denied", "toegang geweigerd", "checking your browser", "attention required",
    )
    signal = next((item for item in hard_signals if item in f"{title} {visible}"), None)
    challenge_element = soup.select_one("#cf-chl-widget, .cf-challenge, #challenge-form, [name='cf-turnstile-response']")
    has_listing_link = any(re.search(r"/(?:woning|woningen|aanbod|detail|appartement|huis)-?", anchor.get("href", ""), re.I) for anchor in soup.find_all("a", href=True))
    soft_challenge = any(word in visible for word in ("captcha", "cloudflare")) and len(visible) < 2000 and not has_listing_link
    cookie_wall = "accepteer cookies om verder te gaan" in visible and len(visible) < 2000
    if signal or challenge_element is not None or soft_challenge or cookie_wall:
        reason = signal or ("captcha/cloudflare" if soft_challenge else "challenge-element")
        if cookie_wall:
            reason = "cookiemuur"
        exc = BlockedPageError(f"{source}: blokkade bij {url} ({reason})")
        setattr(exc, "html", html)
        raise exc
    if len(visible) < 20:
        raise InvalidHtmlResponseError(f"{source}: opvallend korte HTML bij {url}")


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    })
    return session


SESSION = build_session()


def write_debug_artifact(source: str, url: str, error: str, html: str = "") -> Path:
    DEBUG_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9_-]", "-", source.lower())[:40] or "source"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = DEBUG_DIR / f"{slug}-{stamp}.json"
    safe_url = re.sub(r"(?i)(token|key|auth|secret)=[^&]+", r"\1=***", url)
    fragment = re.sub(r"(?i)(token|secret|authorization)[=: /_-]*[A-Za-z0-9:_-]{8,}", r"\1=***", html[:100000])
    path.write_text(json.dumps({"source": source, "url": safe_url, "timestamp_utc": datetime.now(UTC).isoformat(), "error": error[:1000], "html_fragment": fragment}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def extract_vbt_embedded(base_url: str, html: str) -> list[Listing]:
    """Extract houses from VBT's server-rendered Sapper payload."""
    listings: list[Listing] = []
    url_pattern = re.compile(r'url:\s*"([^"\n]*(?:\\u002F|/)woning(?:\\u002F|/)[^"\n]+)"', re.I)
    for match in url_pattern.finditer(html):
        start = html.rfind("address:{", 0, match.start())
        if start < 0:
            continue
        segment = html[start:match.end()]
        raw_url = match.group(1).replace("\\u002F", "/").replace("\\/", "/")
        url = urljoin(base_url, raw_url)
        slug = urlsplit(url).path.rstrip("/").split("/")[-1]
        title = clean_text(slug.replace("-", " ")).title()
        house_match = re.search(r'house:\s*"([^"]+)"', segment)
        city_match = re.search(r'city:\s*"([^"]+)"', segment)
        rent_match = re.search(r'rental:\s*\{price:\s*([0-9]{3,5})', segment)
        area_match = re.search(r'plot:\s*([0-9]{1,4})', segment)
        rooms_match = re.search(r'rooms:\s*([0-9]{1,2})', segment)
        acceptance_match = re.search(r'acceptance:\s*"([^"]+)"', segment)
        city = city_match.group(1) if city_match else ("Utrecht" if "utrecht" in slug.lower() else None)
        if house_match:
            title = clean_text(house_match.group(1))
            if city:
                title = f"{title}, {city}"
        listings.append(Listing(
            source="vbt", title=title[:140], url=url, city=city,
            rent=int(rent_match.group(1)) if rent_match else None,
            area=int(area_match.group(1)) if area_match else None,
            rooms=int(rooms_match.group(1)) if rooms_match else None,
            availability="unknown", available_from=acceptance_match.group(1) if acceptance_match else None,
            postcode=parse_postcode(segment),
        ))
    return dedupe_listings(listings)


def scrape_generic(source: str, urls: list[str], options: Optional[dict] = None) -> SourceResult:
    options = options or {}
    result = SourceResult(source=source)
    all_items: list[Listing] = []
    unexpected_empty_pages: list[tuple[str, str]] = []
    for url in urls:
        result.urls_attempted += 1
        try:
            html = fetch(url, source=source)
            result.requests_ok += 1
            soup = BeautifulSoup(html, "lxml")
            items = extract_from_blocks(source, url, soup)
            if source.lower() == "vbt":
                items = dedupe_listings([*items, *extract_vbt_embedded(url, html)])
                filters = options.get("_filters")
                if filters:
                    items = [item for item in items if is_utrecht_location(item, filters)]
            result.raw_candidates += len(items)
            all_items.extend(items)
            if not items and not re.search(r"geen (?:woningen|resultaten|aanbod)|0 resultaten", html, re.I):
                unexpected_empty_pages.append((url, html))
        except BlockedPageError as e:
            result.requests_failed += 1
            result.blocked = True
            result.errors.append(str(e))
            write_debug_artifact(source, url, str(e), getattr(e, "html", ""))
        except (requests.RequestException, RuntimeError) as e:
            result.requests_failed += 1
            result.errors.append(str(e))
            write_debug_artifact(source, url, str(e))
            logging.warning("%s mislukt voor %s: %s", source, url, e)
    if not all_items:
        for url, html in unexpected_empty_pages:
            error = f"geen kandidaten voor {url}; website-structuur mogelijk gewijzigd"
            result.errors.append(error)
            write_debug_artifact(source, url, error, html)
    result.listings = dedupe_listings(all_items)
    if options.get("enrich_details", False):
        result.listings = enrich_listing_details(source, result.listings, result, max(0, int(options.get("max_detail_requests", 5))))
    result.raw_candidates = max(result.raw_candidates, len(all_items))
    result.finalize()
    return result

def fetch(url: str, source: str = "unknown") -> str:
    last_error = None
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=(8, 25))
            if r.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                retry_after = r.headers.get("Retry-After", "")
                time.sleep(min(float(retry_after), 30) if retry_after.isdigit() else 1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            if "text/html" not in r.headers.get("Content-Type", "text/html"):
                raise RuntimeError(f"onverwacht content-type: {r.headers.get('Content-Type')}")
            validate_html_response(r.text, source=source, url=url)
            return r.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"ophalen mislukt na 3 pogingen: {last_error}")


def extract_jsonld_listings(source: str, base_url: str, soup: BeautifulSoup) -> list[Listing]:
    listings = []

    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
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
            rent = parse_rent(text)
            area = parse_area(text)
            availability, available_from = parse_availability(text)

            city = None
            if isinstance(address, dict):
                city = address.get("addressLocality")
            absolute_url = urljoin(base_url, str(url)) if url else ""
            if name and url and is_valid_listing_url(source, absolute_url, base_url):
                listings.append(
                    Listing(
                        source=source,
                        title=clean_text(str(name))[:140],
                        url=absolute_url,
                        city=city,
                        rent=rent,
                        area=area,
                        rooms=parse_rooms(text),
                        availability=availability,
                        available_from=available_from,
                        postcode=parse_postcode(text),
                    )
                )

    return dedupe_listings(listings)


def extract_from_blocks(source: str, base_url: str, soup: BeautifulSoup) -> list[Listing]:
    listings: list[Listing] = []

    listings.extend(extract_jsonld_listings(source, base_url, soup))

    for block in source_listing_blocks(source, soup):
        text = clean_text(block.get_text(" "))
        rent = parse_rent(text)
        area = parse_area(text)
        rooms = parse_rooms(text)

        links = block.find_all("a", href=True) if hasattr(block, "find_all") else []
        if not links:
            continue

        best_link = links[0]
        patterns = SOURCE_LINK_PATTERNS.get(source.lower(), ())
        for a in links:
            href = a.get("href", "")
            if patterns and any(re.search(pattern, href, re.I) for pattern in patterns):
                best_link = a
                break
            if not patterns and any(word in href.lower() for word in ["woning", "huur", "aanbod", "appartement", "object"]):
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

        city = "Utrecht" if "utrecht" in f"{text} {url}".lower() else None
        availability, available_from = parse_availability(text)

        listings.append(
            Listing(
                source=source,
                title=title[:140],
                url=url,
                city=city,
                rent=rent,
                area=area,
                rooms=rooms,
                availability=availability,
                available_from=available_from,
                postcode=parse_postcode(text),
            )
        )

    return dedupe_listings(listings)


SOURCE_CARD_SELECTORS = {
    "funda": ("article", "[data-test-id='search-result-item']", "[data-testid='search-result-item']"),
    "pararius": (".search-list__item", ".listing-search-item", "article"),
    "vesteda": (".property-card", ".woning-card", "article", "[data-testid='property-card']"),
}


def extract_specialized_listings(source: str, base_url: str, soup: BeautifulSoup) -> list[Listing]:
    """Parse known result-card boundaries; JSON-LD remains a complementary channel."""
    listings = extract_jsonld_listings(source, base_url, soup)
    patterns = SOURCE_LINK_PATTERNS[source]
    seen_blocks: set[int] = set()
    for selector in SOURCE_CARD_SELECTORS[source]:
        for block in soup.select(selector):
            if id(block) in seen_blocks:
                continue
            seen_blocks.add(id(block))
            link = next((a for a in block.find_all("a", href=True) if any(re.search(pattern, a.get("href", ""), re.I) for pattern in patterns)), None)
            if link is None:
                continue
            text = clean_text(block.get_text(" "))
            heading = block.find(["h1", "h2", "h3", "h4"])
            title = clean_text(heading.get_text(" ") if heading else link.get_text(" ")) or text[:90]
            url = urljoin(base_url, link["href"])
            availability, available_from = parse_availability(text)
            postcode = parse_postcode(text)
            city = "Utrecht" if "utrecht" in f"{text} {url}".lower() or (postcode and postcode[:4] in UTRECHT_POSTCODE_PREFIXES) else None
            listings.append(Listing(source, title[:140], url, city, parse_rent(text), parse_area(text), parse_rooms(text), availability, available_from, postcode))
    return dedupe_listings(listings)


def pagination_url(source: str, url: str, page: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if source == "funda":
        query["search_result"] = str(page)
        path = parts.path
    elif source == "pararius":
        path = re.sub(r"/page-\d+$", "", parts.path.rstrip("/")) + f"/page-{page}"
    else:
        query["page"] = str(page)
        path = parts.path
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))


def extract_detail_listing(listing: Listing, html: str) -> Listing:
    soup = BeautifulSoup(html, "lxml")
    candidates = extract_jsonld_listings(listing.source, listing.url, soup)
    merged = listing
    for candidate in candidates:
        if canonical_url(candidate.url) == canonical_url(listing.url):
            merged = merge_listings(merged, candidate)
    text = clean_text(soup.get_text(" "))
    heading = clean_text(soup.find("h1").get_text(" ") if soup.find("h1") else listing.title)[:140]
    postcode = parse_postcode(text)
    location_probe = Listing(listing.source, heading, listing.url, listing.city, postcode=postcode)
    city = listing.city or ("Utrecht" if is_utrecht_location(location_probe, {"city": "Utrecht"}) else None)
    availability, available_from = parse_availability(text)
    detail = Listing(
        listing.source,
        heading,
        listing.url,
        city,
        parse_rent(text), parse_area(text), parse_rooms(text), availability,
        available_from, postcode,
    )
    return merge_listings(merged, detail)


def enrich_listing_details(source: str, listings: list[Listing], result: SourceResult, max_requests: int) -> list[Listing]:
    enriched: list[Listing] = []
    used = 0
    for listing in listings:
        needs_detail = listing.rent is None or listing.area is None or listing.availability == "unknown"
        if not needs_detail or used >= max_requests or listing.availability in {"reserved", "rented"}:
            enriched.append(listing)
            continue
        used += 1
        result.urls_attempted += 1
        result.detail_requests += 1
        try:
            html = fetch(listing.url, source=source)
            result.requests_ok += 1
            enriched.append(extract_detail_listing(listing, html))
        except BlockedPageError as exc:
            result.requests_failed += 1
            result.blocked = True
            result.errors.append(f"detail {listing.url}: {exc}")
            enriched.append(listing)
        except (requests.RequestException, RuntimeError) as exc:
            result.requests_failed += 1
            result.errors.append(f"detail {listing.url}: {exc}")
            enriched.append(listing)
    return enriched


def scrape_specialized(source: str, urls: list[str], options: Optional[dict] = None) -> SourceResult:
    options = options or {}
    max_pages = max(1, int(options.get("max_pages", 2)))
    result = SourceResult(source=source)
    all_items: list[Listing] = []
    known_urls: set[str] = set()
    stop_source = False
    for base_url in urls:
        for page in range(1, max_pages + 1):
            url = base_url if page == 1 else pagination_url(source, base_url, page)
            result.urls_attempted += 1
            try:
                html = fetch(url, source=source)
                result.requests_ok += 1
                items = extract_specialized_listings(source, url, BeautifulSoup(html, "lxml"))
                result.raw_candidates += len(items)
                new_items = [item for item in items if canonical_url(item.url) not in known_urls]
                if not new_items:
                    if page == 1 and not re.search(r"geen (?:woningen|resultaten|aanbod)|0 resultaten", html, re.I):
                        error = f"geen kandidaten voor {url}; bronselectors mogelijk gewijzigd"
                        result.errors.append(error)
                        write_debug_artifact(source, url, error, html)
                    break
                all_items.extend(new_items)
                known_urls.update(canonical_url(item.url) for item in new_items)
            except BlockedPageError as exc:
                result.requests_failed += 1; result.blocked = True; result.errors.append(str(exc)); write_debug_artifact(source, url, str(exc), getattr(exc, "html", "")); stop_source = True; break
            except (requests.RequestException, RuntimeError) as exc:
                result.requests_failed += 1; result.errors.append(str(exc)); write_debug_artifact(source, url, str(exc)); break
        if stop_source:
            break
    if options.get("enrich_details", True):
        all_items = enrich_listing_details(source, dedupe_listings(all_items), result, max(0, int(options.get("max_detail_requests", 10))))
    result.listings = dedupe_listings(all_items)
    result.finalize()
    return result


def scrape_funda(source: str, urls: list[str], options: Optional[dict] = None) -> SourceResult:
    return scrape_specialized(source, urls, options)


def scrape_pararius(source: str, urls: list[str], options: Optional[dict] = None) -> SourceResult:
    return scrape_specialized(source, urls, options)


def scrape_vesteda(source: str, urls: list[str], options: Optional[dict] = None) -> SourceResult:
    return scrape_specialized(source, urls, options)


SCRAPERS = {"funda": scrape_funda, "pararius": scrape_pararius, "vesteda": scrape_vesteda}
LAST_SOURCE_RESULTS: list[SourceResult] = []


def scrape_all(config: dict) -> list[Listing]:
    global LAST_SOURCE_RESULTS
    results: list[Listing] = []
    source_results: list[SourceResult] = []
    sources = config.get("sources", {})

    for source, cfg in sources.items():
        if not cfg or not cfg.get("enabled", False):
            continue

        urls = cfg.get("urls", [])
        logging.info("Check %s (%d url's)", source, len(urls))
        options = {**cfg, "_filters": config.get("filters", {})}
        source_result = SCRAPERS.get(source.lower(), scrape_generic)(source, urls, options)
        source_result.finalize(config.get("filters", {}))
        logging.info("Bron %s: requests %d/%d, kandidaten %d, uniek %d, compleet %d, matches %d, status %s", source, source_result.requests_ok, source_result.urls_attempted, source_result.raw_candidates, source_result.unique_listings, source_result.complete_listings, source_result.matches, source_result.status)
        source_results.append(source_result)
        results.extend(source_result.listings)

    LAST_SOURCE_RESULTS = source_results
    return dedupe_listings(results)


def filter_reasons(l: Listing, filters: dict) -> list[str]:
    reasons: list[str] = []
    city = (filters.get("city") or "").lower()
    max_rent = int(filters.get("max_rent", 999999))
    min_area = int(filters.get("min_area", 0))

    if city and city == "utrecht" and not is_utrecht_location(l, filters):
        reasons.append("wrong_city")
    elif city and city != "utrecht" and city not in f"{l.title} {l.city or ''} {l.url}".lower():
        reasons.append("wrong_city")
    if l.availability in {"reserved", "rented"}:
        reasons.append("unavailable")
    if l.rent is None:
        if not filters.get("allow_missing_rent", False):
            reasons.append("missing_rent")
    elif l.rent > max_rent:
        reasons.append("rent_too_high")
    if l.area is None:
        if not filters.get("allow_missing_area", False):
            reasons.append("missing_area")
    elif l.area < min_area:
        reasons.append("area_too_small")
    return reasons


def matches_filters(l: Listing, filters: dict) -> bool:
    return not filter_reasons(l, filters)


def classify_listing(l: Listing, filters: dict) -> str:
    reasons = filter_reasons(l, filters)
    hard_reasons = {"wrong_city", "rent_too_high", "area_too_small", "unavailable"}
    if hard_reasons.intersection(reasons):
        return "rejected"
    if any(reason in {"missing_rent", "missing_area"} for reason in reasons) or l.availability == "unknown":
        return "possible_match"
    return "match"


def _legacy_format_message(l: Listing) -> str:
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


def format_message(l: Listing, classification: str = "match") -> str:
    heading = "🔎 Mogelijke huurwoning — controleer ontbrekende gegevens" if classification == "possible_match" else "🏠 Nieuwe huurwoning"
    rent = f"€{l.rent:,} p/m".replace(",", ".") if l.rent is not None else "onbekend ⚠️"
    area = f"{l.area} m²" if l.area is not None else "onbekend ⚠️"
    parts = [heading, "", f"Bron: {l.source}", f"Titel: {l.title}", f"Prijs: {rent}", f"Oppervlakte: {area}"]
    if l.rooms is not None:
        parts.append(f"Kamers: {l.rooms}")
    if l.city:
        parts.append(f"Plaats: {l.city}")
    if l.postcode:
        parts.append(f"Postcode: {l.postcode}")
    availability_labels = {"available": "beschikbaar", "reserved": "gereserveerd", "rented": "verhuurd", "unknown": "onbekend ⚠️"}
    parts.append(f"Status: {availability_labels[l.availability]}")
    if l.available_from:
        parts.append(f"Beschikbaar vanaf: {l.available_from}")
    parts.extend(["", canonical_url(l.url)])
    return "\n".join(parts)


def send_telegram(config: dict, text: str) -> None:
    token = str(config["telegram"]["token"]).strip()
    chat_id = str(config["telegram"]["chat_id"]).strip()
    placeholders = {"", "VIA_GITHUB_SECRET", "VUL_HIER_IN"}
    if token in placeholders or chat_id in placeholders:
        raise RuntimeError("Telegram token/chat_id ontbreken in config.yaml")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    last_error = None
    for attempt in range(3):
        try:
            r = SESSION.post(url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False}, timeout=(8, 20))
            r.raise_for_status()
            result = r.json()
            if not result.get("ok"):
                raise RuntimeError(result.get("description", "Telegram gaf geen bevestiging"))
            return
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Telegram versturen mislukt na 3 pogingen: {last_error}")


def calculate_run_status(results: list[SourceResult]) -> str:
    if not results or not any(result.status in {"healthy", "partial", "parsing_warning"} for result in results):
        return "failed"
    if any(result.status != "healthy" or result.errors for result in results):
        return "warning"
    return "healthy"


def markdown_escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("<", "&lt;").replace(">", "&gt;")


def write_github_summary(results: list[SourceResult], rejections: Counter, sent: int, run_status: str) -> None:
    target = os.getenv("GITHUB_STEP_SUMMARY")
    if not target:
        return
    lines = [
        "# Utrecht Huur Bot", "", "## Resultaat", "",
        f"- Tijdstip (UTC): {datetime.now(UTC).isoformat()}", f"- Runstatus: {run_status}",
        f"- Totaal gevonden: {sum(r.raw_candidates for r in results)}", f"- Totaal uniek: {sum(r.unique_listings for r in results)}",
        f"- Volledige listings: {sum(r.complete_listings for r in results)}", f"- Filtermatches: {sum(r.matches for r in results)}", f"- Mogelijke matches: {sum(r.possible_matches for r in results)}", f"- Nieuwe meldingen: {sent}",
        "", "## Bronnen", "", "| Bron | Requests | Details | Kandidaten | Uniek | Beschikbaar | Compleet | Matches | Mogelijk | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for result in results:
        lines.append(f"| {markdown_escape(result.source)} | {result.requests_ok}/{result.urls_attempted} | {result.detail_requests} | {result.raw_candidates} | {result.unique_listings} | {result.available} | {result.complete_listings} | {result.matches} | {result.possible_matches} | {result.status} |")
    labels = {"wrong_city": "Verkeerde plaats", "missing_rent": "Prijs ontbreekt", "rent_too_high": "Te duur", "missing_area": "Oppervlakte ontbreekt", "area_too_small": "Te klein", "unavailable": "Niet beschikbaar"}
    lines.extend(["", "## Afwijzingen", "", "| Reden | Aantal |", "|---|---:|"])
    lines.extend(f"| {label} | {rejections[key]} |" for key, label in labels.items())
    errors = [error for result in results for error in result.errors]
    if errors:
        lines.extend(["", "## Waarschuwingen en fouten", ""] + [f"- {markdown_escape(error)}" for error in errors])
    try:
        with open(target, "a", encoding="utf-8") as summary:
            summary.write("\n".join(lines) + "\n")
    except OSError as exc:
        logging.warning("GitHub Summary kon niet geschreven worden: %s", exc)


def update_source_state(config: dict, seen: set[str], results: list[SourceResult]) -> None:
    state = getattr(seen, "state", None)
    if state is None:
        return
    sources = state.setdefault("sources", {})
    options = config.get("notifications", {})
    now = datetime.now(UTC)
    repeat = timedelta(hours=float(options.get("source_failure_repeat_hours", 12)))
    for result in results:
        previous = sources.get(result.source, {})
        broken = result.status in {"failed", "blocked"}
        if broken:
            failures = int(previous.get("consecutive_failures", 0)) + 1
            fingerprint = hashlib.sha256((result.status + "|" + "|".join(result.errors)).encode()).hexdigest()[:16]
            last_notice = previous.get("last_error_notification_at")
            due = True
            if last_notice:
                try:
                    due = now - datetime.fromisoformat(last_notice) >= repeat
                except ValueError:
                    pass
            notify = options.get("notify_on_source_failure", True) and (result.blocked or failures >= 2) and (failures == 2 or previous.get("error_fingerprint") != fingerprint or due)
            if notify:
                send_telegram(config, f"⚠️ Bronprobleem: {result.source}\nStatus: {result.status}\n" + "\n".join(result.errors[:3]))
                last_notice = now.isoformat()
            sources[result.source] = {**previous, "consecutive_failures": failures, "last_error": result.errors[-1] if result.errors else result.status, "error_fingerprint": fingerprint, "last_failure_at": now.isoformat(), "last_error_notification_at": last_notice}
        else:
            if previous.get("consecutive_failures", 0) and options.get("notify_on_recovery", True):
                send_telegram(config, f"✅ Bron hersteld: {result.source}")
            sources[result.source] = {**previous, "consecutive_failures": 0, "last_error": None, "error_fingerprint": None, "last_success_at": now.isoformat()}


def cleanup_stale_state(seen: set[str], healthy_sources: set[str], days: int) -> int:
    state = getattr(seen, "state", None)
    if state is None:
        return 0
    threshold = datetime.now(UTC) - timedelta(days=days)
    removed = 0
    for uid, record in list(state.get("listings", {}).items()):
        if record.get("source") not in healthy_sources:
            continue
        try:
            last_seen = datetime.fromisoformat(record["last_seen_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if last_seen < threshold:
            del state["listings"][uid]
            seen.discard(uid)
            removed += 1
    return removed


def check_once(
    config: dict,
    seen: set[str],
    first_run: bool = False,
) -> tuple[int, int]:
    listings = scrape_all(config)
    filters = config.get("filters", {})

    classifications = {listing.uid: classify_listing(listing, filters) for listing in listings}
    exact_matches = [listing for listing in listings if classifications[listing.uid] == "match"]
    possible_matches = [listing for listing in listings if classifications[listing.uid] == "possible_match"]
    send_possible = config.get("notifications", {}).get("send_possible_matches", True)
    matches = exact_matches + (possible_matches if send_possible else [])
    rejections = Counter(reason for listing in listings for reason in filter_reasons(listing, filters))

    missing_rent = sum(
        listing.rent is None
        for listing in listings
    )

    missing_area = sum(
        listing.area is None
        for listing in listings
    )

    max_rent = int(filters.get("max_rent", 999999))
    min_area = int(filters.get("min_area", 0))

    over_max_rent = sum(
        listing.rent is not None
        and listing.rent > max_rent
        for listing in listings
    )

    under_min_area = sum(
        listing.area is not None
        and listing.area < min_area
        for listing in listings
    )
    wrong_city = rejections["wrong_city"]
    unavailable = rejections["unavailable"]

    logging.info(
        "%d listings gevonden, %d voldoen aan filters | "
        "prijs ontbreekt: %d | oppervlakte ontbreekt: %d | "
        "te duur: %d | te klein: %d",
        len(listings),
        len(matches),
        missing_rent,
        missing_area,
        over_max_rent,
        under_min_area,
    )

    sent = 0
    notify_existing = bool(
        config.get("notify_existing_on_first_run", True)
    )
    state = getattr(seen, "state", None)
    state_listings = state.setdefault("listings", {}) if state is not None else {}
    now = datetime.now(UTC).isoformat()

    for listing in matches:
        previous = state_listings.get(listing.uid)
        previous_rent = previous.get("rent") if previous else None
        notify_options = config.get("notifications", {})
        price_drop = previous_rent is not None and listing.rent is not None and listing.rent < previous_rent
        price_increase = previous_rent is not None and listing.rent is not None and listing.rent > previous_rent
        should_notify = listing.uid not in seen
        current_classification = classifications.get(listing.uid, "match")
        should_notify = should_notify or ((previous or {}).get("classification") == "possible_match" and current_classification == "match")
        should_notify = should_notify or (price_drop and notify_options.get("notify_on_price_drop", True))
        should_notify = should_notify or (price_increase and notify_options.get("notify_on_price_increase", False))
        if not should_notify:
            if previous is not None:
                previous["last_seen_at"] = now
                if listing.rent is not None:
                    previous["rent"] = listing.rent
            continue

        if first_run and not notify_existing:
            seen.add(listing.uid)
            if state is not None:
                state_listings[listing.uid] = {
                    "source": listing.source, "url": canonical_url(listing.url), "title": listing.title,
                    "rent": listing.rent, "area": listing.area, "rooms": listing.rooms, "city": listing.city,
                    "availability": listing.availability, "available_from": listing.available_from, "postcode": listing.postcode,
                    "classification": current_classification,
                    "first_seen_at": now, "last_seen_at": now, "last_notified_at": None,
                }
            logging.info(
                "Bestaande match gemarkeerd als gezien: %s",
                listing.title,
            )
            continue

        logging.info(
            "Nieuwe match: %s | €%s | %sm2",
            listing.title,
            listing.rent,
            listing.area,
        )

        send_telegram(config, format_message(listing, classifications.get(listing.uid, "match")))

        # Pas als Telegram succesvol is, markeren we de woning als gezien.
        seen.add(listing.uid)
        if state is not None:
            state_listings[listing.uid] = {
                "source": listing.source, "url": canonical_url(listing.url), "title": listing.title,
                "rent": listing.rent if listing.rent is not None else previous_rent,
                "area": listing.area if listing.area is not None else (previous or {}).get("area"),
                "rooms": listing.rooms if listing.rooms is not None else (previous or {}).get("rooms"),
                "city": listing.city or (previous or {}).get("city"),
                "availability": listing.availability, "available_from": listing.available_from or (previous or {}).get("available_from"),
                "postcode": listing.postcode or (previous or {}).get("postcode"),
                "classification": current_classification,
                "first_seen_at": (previous or {}).get("first_seen_at", now), "last_seen_at": now, "last_notified_at": now,
            }
        save_seen(seen)
        sent += 1

    notifications = config.get("notifications", {})
    send_no_new = notifications.get("send_no_new_summary", config.get("send_summary_when_no_new", False))
    if bool(send_no_new) and sent == 0:
        send_telegram(
            config,
            f"✅ Bot actief\n"
            f"{len(listings)} listings gevonden\n"
            f"{len(exact_matches)} exacte matches\n"
            f"{len(possible_matches)} mogelijke matches\n\n"
            f"Diagnose:\n"
            f"• Verkeerde plaats: {wrong_city}\n"
            f"• Prijs ontbreekt: {missing_rent}\n"
            f"• Oppervlakte ontbreekt: {missing_area}\n"
            f"• Boven maximale huur: {over_max_rent}\n"
            f"• Onder minimale oppervlakte: {under_min_area}\n\n"
            f"• Niet beschikbaar: {unavailable}\n\n"
            f"{sent} nieuwe meldingen verstuurd",
        )

    seen.add(STATE_VERSION_MARKER)
    save_seen(seen)

    source_results = LAST_SOURCE_RESULTS if config.get("sources") else []
    run_status = calculate_run_status(source_results) if source_results else "healthy"
    update_source_state(config, seen, source_results)
    healthy_sources = {result.source for result in source_results if result.status in {"healthy", "partial", "parsing_warning"}}
    removed = cleanup_stale_state(seen, healthy_sources, int(config.get("state", {}).get("stale_after_days", 60)))
    if removed:
        logging.info("%d verouderde state-records verwijderd", removed)
    save_seen(seen)
    if source_results:
        write_github_summary(source_results, rejections, sent, run_status)
    if source_results and run_status == "failed":
        raise ScrapeHealthError("alle actieve bronnen zijn technisch mislukt of geblokkeerd")

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
    # Existing releases used unstable IDs containing price and area. On the
    # first run after this upgrade, seed the new URL IDs without flooding chat.
    first = not STATE_FILE.exists() or STATE_VERSION_MARKER not in seen

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
