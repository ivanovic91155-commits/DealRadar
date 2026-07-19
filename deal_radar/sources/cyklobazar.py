from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

from deal_radar.config import CyklobazarProfile
from deal_radar.http import HttpError, get_bytes
from deal_radar.models import Listing
from deal_radar.sources.bazos import matches_profile


PRAGUE_TZ = ZoneInfo("Europe/Prague")
LOGGER = logging.getLogger(__name__)
AD_PATH_RE = re.compile(r"/inzerat/([A-Za-z0-9_-]{5,})/([^?#]+)")
PRICE_RE = re.compile(
    r"(?<!\d)(\d(?:[\d .\u00a0]*\d)?)\s*(Kč|CZK|,-)(?!\w)",
    re.IGNORECASE,
)
TIME_PATTERNS = (
    (re.compile(r"\bpřed\s+(\d+)\s+minut", re.IGNORECASE), "minutes"),
    (re.compile(r"\bpřed\s+(\d+)\s+hodin", re.IGNORECASE), "hours"),
    (re.compile(r"\bpřed\s+(\d+)\s+d(?:ny|nem|ní)", re.IGNORECASE), "days"),
)
BLOCK_TAGS = {"article", "div", "h1", "h2", "h3", "h4", "li", "p", "section", "time"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
TITLE_HINTS = ("title", "name", "headline")
DESCRIPTION_HINTS = ("description", "summary", "excerpt", "text")
LOCATION_HINTS = ("location", "locality", "place", "city", "district")
PRICE_HINTS = ("price", "cena")


@dataclass(slots=True)
class ParsedPrice:
    amount: int | None = None
    currency: str = "CZK"
    raw_text: str = ""
    status: str = "missing"
    origin: str = "not_found"


@dataclass(slots=True)
class CyklobazarFetchStats:
    found: int = 0
    parsed: int = 0
    numeric_list_page: int = 0
    numeric_detail_page: int = 0
    negotiable: int = 0
    in_description: int = 0
    free: int = 0
    missing: int = 0
    parse_errors: int = 0
    detail_errors: int = 0


def parse_price_text(value: str, origin: str = "list_page") -> ParsedPrice:
    raw = " ".join(value.replace("\u00a0", " ").split()).strip()
    normalized = raw.casefold()
    if re.search(r"\b(?:cena\s+)?dohodou\b", normalized):
        return ParsedPrice(raw_text=raw, status="negotiable", origin=origin)
    if re.search(r"\bzdarma\b", normalized):
        return ParsedPrice(raw_text=raw, status="free", origin=origin)
    if re.search(r"\bcena\s+v\s+textu\b", normalized):
        return ParsedPrice(raw_text=raw, status="in_description", origin=origin)
    match = PRICE_RE.search(raw)
    if match:
        digits = re.sub(r"\D", "", match.group(1))
        if digits:
            currency = "CZK" if match.group(2).casefold() in {"kč", "czk", ",-"} else match.group(2).upper()
            return ParsedPrice(
                amount=int(digits),
                currency=currency,
                raw_text=match.group(0).strip(),
                status="numeric",
                origin=origin,
            )
    if raw:
        return ParsedPrice(raw_text=raw, status="parse_error", origin=origin)
    return ParsedPrice()


def _clean_text(parts: list[str]) -> str:
    lines = [" ".join(line.split()) for line in "".join(parts).splitlines()]
    return "\n".join(line for line in lines if line)


def _class_value(attrs: dict[str, str | None]) -> str:
    return " ".join(
        value or "" for key, value in attrs.items() if key in {"class", "id", "itemprop"}
    ).casefold()


@dataclass(slots=True)
class _Card:
    href: str
    external_id: str
    slug: str
    link_title: str = ""
    all_parts: list[str] = field(default_factory=list)
    heading_parts: list[str] = field(default_factory=list)
    title_parts: list[str] = field(default_factory=list)
    description_parts: list[str] = field(default_factory=list)
    location_parts: list[str] = field(default_factory=list)
    price_parts: list[str] = field(default_factory=list)
    image_url: str | None = None
    attribute_price: int | None = None
    attribute_currency: str = "CZK"
    promoted: bool = False

    @property
    def text(self) -> str:
        return _clean_text(self.all_parts)


class _ListingLinkParser(HTMLParser):
    """Collect listing cards without relying on Cyklobazar's CSS class names.

    The public category pages wrap each result in a link to /inzerat/<id>/<slug>.
    Class and semantic hints are used when available, with the URL slug as a safe
    fallback so routine frontend class changes do not silently erase the title.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.cards: list[_Card] = []
        self.current: _Card | None = None
        self.depth = 0
        self.stack: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        tag = tag.casefold()
        if self.current is None and tag == "a":
            href = attributes.get("href") or ""
            match = AD_PATH_RE.search(href)
            if match:
                onclick = str(attributes.get("onclick") or "")
                if re.search(r'"pageTypeReferrer"\s*:\s*"headerCategories"', onclick):
                    return
                attribute_price = re.search(r'"price"\s*:\s*(\d+(?:\.\d+)?)', onclick)
                attribute_currency = re.search(r'"currency"\s*:\s*"([A-Za-z]{3})"', onclick)
                class_hint = _class_value(attributes)
                self.current = _Card(
                    href=urljoin(self.base_url, href),
                    external_id=match.group(1),
                    slug=unquote(match.group(2)).strip("/"),
                    link_title=str(attributes.get("title") or attributes.get("aria-label") or ""),
                    attribute_price=int(float(attribute_price.group(1))) if attribute_price else None,
                    attribute_currency=attribute_currency.group(1).upper() if attribute_currency else "CZK",
                    promoted=(
                        "pinned" in class_hint
                        or bool(re.search(r'"event"\s*:\s*"clickOnTopProduct"', onclick))
                    ),
                )
                self.depth = 1
                self.stack = [(tag, _class_value(attributes))]
                self._capture_media(tag, attributes)
                return
        if self.current is None:
            return
        if tag not in VOID_TAGS:
            self.depth += 1
            self.stack.append((tag, _class_value(attributes)))
        if tag in BLOCK_TAGS:
            self.current.all_parts.append("\n")
        self._capture_media(tag, attributes)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.current is not None:
            self._capture_media(tag.casefold(), dict(attrs))

    def _capture_media(self, tag: str, attrs: dict[str, str | None]) -> None:
        if self.current is None or tag != "img" or self.current.image_url:
            return
        image = attrs.get("src") or attrs.get("data-src") or attrs.get("data-lazy-src")
        if not image and attrs.get("srcset"):
            image = str(attrs["srcset"]).split(",")[0].strip().split(" ")[0]
        if image and not image.startswith("data:"):
            self.current.image_url = urljoin(self.base_url, image)

    def handle_data(self, data: str) -> None:
        if self.current is None or not data.strip():
            return
        self.current.all_parts.append(data)
        tags = {tag for tag, _ in self.stack}
        hints = " ".join(hint for _, hint in self.stack)
        if tags & {"h1", "h2", "h3", "h4"}:
            self.current.heading_parts.append(data)
        if any(hint in hints for hint in TITLE_HINTS):
            self.current.title_parts.append(data)
        if any(hint in hints for hint in DESCRIPTION_HINTS):
            self.current.description_parts.append(data)
        if any(hint in hints for hint in LOCATION_HINTS):
            self.current.location_parts.append(data)
        if any(hint in hints for hint in PRICE_HINTS):
            self.current.price_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        tag = tag.casefold()
        if tag in VOID_TAGS:
            return
        if tag in BLOCK_TAGS:
            self.current.all_parts.append("\n")
        self.depth -= 1
        if self.stack:
            self.stack.pop()
        if self.depth == 0:
            self.cards.append(self.current)
            self.current = None
            self.stack = []


def _parse_price(text: str) -> int | None:
    return parse_price_text(text).amount


class _DetailPriceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.price_depth = 0
        self.price_parts: list[str] = []
        self.in_json_ld = False
        self.json_parts: list[str] = []
        self.json_values: list[object] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        hint = _class_value(attributes)
        if self.price_depth and tag.casefold() not in VOID_TAGS:
            self.price_depth += 1
        elif any(marker in hint for marker in PRICE_HINTS):
            self.price_depth = 1
        if tag.casefold() == "script" and str(attributes.get("type") or "").casefold() == "application/ld+json":
            self.in_json_ld = True
            self.json_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self.in_json_ld:
            self.in_json_ld = False
            try:
                self.json_values.append(json.loads("".join(self.json_parts)))
            except (json.JSONDecodeError, TypeError):
                pass
        if self.price_depth and tag.casefold() not in VOID_TAGS:
            self.price_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_json_ld:
            self.json_parts.append(data)
        if self.price_depth:
            self.price_parts.append(data)


def _json_offer_prices(value: object) -> list[tuple[object, str]]:
    found: list[tuple[object, str]] = []
    if isinstance(value, dict):
        if "price" in value:
            found.append((value.get("price"), str(value.get("priceCurrency") or "CZK")))
        for child in value.values():
            found.extend(_json_offer_prices(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_json_offer_prices(child))
    return found


def parse_detail_price(html_data: bytes) -> ParsedPrice:
    parser = _DetailPriceParser()
    parser.feed(html_data.decode("utf-8", errors="replace"))
    for value in parser.json_values:
        for amount, currency in _json_offer_prices(value):
            if isinstance(amount, (int, float)) and amount >= 0:
                return ParsedPrice(
                    amount=int(round(amount)),
                    currency=currency.upper(),
                    raw_text=f"{amount} {currency.upper()}",
                    status="numeric",
                    origin="detail_page",
                )
            if isinstance(amount, str):
                parsed = parse_price_text(f"{amount} {currency}", "detail_page")
                if parsed.status != "parse_error":
                    return parsed
    raw = _clean_text(parser.price_parts)
    return parse_price_text(raw, "detail_page")


def _relative_datetime(text: str, now: datetime) -> datetime | None:
    normalized = " ".join(text.split()).casefold()
    for pattern, unit in TIME_PATTERNS:
        match = pattern.search(normalized)
        if match:
            return now - timedelta(**{unit: int(match.group(1))})
    if re.search(r"\bpřed\s+hodinou\b", normalized):
        return now - timedelta(hours=1)
    if re.search(r"\bpřed\s+minutou\b", normalized):
        return now - timedelta(minutes=1)
    if re.search(r"\bvčera\b", normalized):
        return now - timedelta(days=1)
    if re.search(r"\bdnes\b", normalized):
        return now
    return None


def _fallback_title(card: _Card) -> str:
    candidates = (
        _clean_text(card.heading_parts),
        _clean_text(card.title_parts),
        card.link_title.strip(),
    )
    for candidate in candidates:
        line = candidate.splitlines()[0] if candidate else ""
        if line and not PRICE_RE.fullmatch(line) and line.casefold() not in {"top", "inzerát"}:
            return re.sub(r"^TOP\s+", "", line, flags=re.IGNORECASE).strip()[:300]
    slug = urlparse(card.href).path.rstrip("/").rsplit("/", 1)[-1]
    return " ".join(part for part in unquote(slug).replace("_", "-").split("-") if part)[:300]


def _location(card: _Card, fallback: str) -> str | None:
    location = _clean_text(card.location_parts)
    if location:
        return location.splitlines()[0][:120]
    return fallback[:120] or None


def parse_html(
    html_data: bytes,
    profile: CyklobazarProfile,
    *,
    now: datetime | None = None,
) -> list[Listing]:
    preview = html_data[:10_000].decode("utf-8", errors="replace")
    if "cf-mitigated" in preview.casefold() or "just a moment" in preview.casefold():
        raise HttpError(
            "Cyklobazar returned a Cloudflare browser check. "
            "No protection bypass was attempted; use an allowed feed/API or run from an approved connection."
        )
    parser = _ListingLinkParser(profile.url)
    parser.feed(html_data.decode("utf-8", errors="replace"))
    now = now or datetime.now(PRAGUE_TZ)

    # The same listing can have separate image and text links. Keep the richest
    # representation and fill a missing image from its duplicate.
    richest: dict[str, _Card] = {}
    for card in parser.cards:
        existing = richest.get(card.external_id)
        if existing is None or len(card.text) > len(existing.text):
            if existing and not card.image_url:
                card.image_url = existing.image_url
            richest[card.external_id] = card
        elif not existing.image_url and card.image_url:
            existing.image_url = card.image_url

    listings: list[Listing] = []
    for card in richest.values():
        text = card.text
        published_at = _relative_datetime(text, now)
        promoted = card.promoted or bool(re.search(r"(?:^|\n)\s*TOP\b", text, re.IGNORECASE))
        if promoted and published_at is None and not profile.include_promoted:
            continue
        title = _fallback_title(card)
        if not title:
            continue
        description = _clean_text(card.description_parts) or text
        raw_price = _clean_text(card.price_parts)
        price = parse_price_text(raw_price, "list_page")
        if price.status in {"missing", "parse_error"} and card.attribute_price is not None:
            price = ParsedPrice(
                amount=card.attribute_price,
                currency=card.attribute_currency,
                raw_text=f"{card.attribute_price} {card.attribute_currency}",
                status="numeric",
                origin="list_page",
            )
        listing = Listing(
            source="cyklobazar",
            external_id=card.external_id,
            title=title,
            description=description[:2000],
            url=card.href,
            profile=profile.name,
            price_czk=price.amount if price.currency == "CZK" else None,
            location=_location(card, profile.location_label),
            published_at=published_at,
            image_url=card.image_url,
            price_amount=price.amount,
            currency=price.currency,
            raw_price_text=price.raw_text,
            price_status=price.status,
            price_origin=price.origin,
        )
        if matches_profile(listing, profile):
            listings.append(listing)
    return listings


class CyklobazarSource:
    source_name = "cyklobazar"

    def __init__(
        self,
        profile: CyklobazarProfile,
        timeout: int = 30,
        *,
        existing_listing: Callable[[str], Listing | None] | None = None,
        fetcher: Callable[..., bytes] = get_bytes,
    ) -> None:
        self.profile = profile
        self.timeout = timeout
        self.existing_listing = existing_listing
        self.fetcher = fetcher
        self.last_stats = CyklobazarFetchStats()

    @property
    def label(self) -> str:
        return self.profile.name

    def fetch(self) -> list[Listing]:
        try:
            html_data = self.fetcher(
                self.profile.url,
                timeout=self.timeout,
                headers={
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                    "Accept-Language": "cs,en;q=0.7",
                },
            )
        except HttpError as exc:
            if "403" in str(exc):
                raise HttpError(
                    "Cyklobazar blocked this connection with Cloudflare (HTTP 403). "
                    "The source needs an allowed feed/API or approval from the site owner."
                ) from exc
            raise
        card_parser = _ListingLinkParser(self.profile.url)
        card_parser.feed(html_data.decode("utf-8", errors="replace"))
        listings = parse_html(html_data, self.profile)
        stats = CyklobazarFetchStats(
            found=len({card.external_id for card in card_parser.cards}),
            parsed=len(listings),
        )
        enriched: list[Listing] = []
        for listing in listings:
            if listing.price_status in {"missing", "parse_error"}:
                existing = self.existing_listing(listing.external_id) if self.existing_listing else None
                if existing is not None:
                    if existing.price_status:
                        listing.price_czk = existing.price_czk
                        listing.price_amount = existing.price_amount
                        listing.currency = existing.currency
                        listing.raw_price_text = existing.raw_price_text
                        listing.price_status = existing.price_status
                        listing.price_origin = existing.price_origin
                else:
                    try:
                        detail = self.fetcher(
                            listing.url,
                            timeout=self.timeout,
                            headers={
                                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                                "Accept-Language": "cs,en;q=0.7",
                            },
                        )
                        price = parse_detail_price(detail)
                        listing.price_czk = price.amount if price.currency == "CZK" else None
                        listing.price_amount = price.amount
                        listing.currency = price.currency
                        listing.raw_price_text = price.raw_text
                        listing.price_status = price.status
                        listing.price_origin = price.origin
                    except Exception as exc:
                        stats.detail_errors += 1
                        LOGGER.warning(
                            "Cyklobazar detail price failed for %s: %s",
                            listing.external_id,
                            type(exc).__name__,
                        )
            if matches_profile(listing, self.profile):
                enriched.append(listing)

        for listing in enriched:
            if listing.price_status == "numeric":
                if listing.price_origin == "detail_page":
                    stats.numeric_detail_page += 1
                else:
                    stats.numeric_list_page += 1
            elif listing.price_status == "negotiable":
                stats.negotiable += 1
            elif listing.price_status == "in_description":
                stats.in_description += 1
            elif listing.price_status == "free":
                stats.free += 1
            elif listing.price_status == "parse_error":
                stats.parse_errors += 1
            else:
                stats.missing += 1
        self.last_stats = stats
        LOGGER.info(
            "Cyklobazar price stats: found=%d parsed=%d list_numeric=%d detail_numeric=%d "
            "negotiable=%d in_description=%d free=%d missing=%d parse_errors=%d detail_errors=%d",
            stats.found,
            stats.parsed,
            stats.numeric_list_page,
            stats.numeric_detail_page,
            stats.negotiable,
            stats.in_description,
            stats.free,
            stats.missing,
            stats.parse_errors,
            stats.detail_errors,
        )
        return enriched
