from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

from deal_radar.config import CyklobazarProfile
from deal_radar.http import HttpError, get_bytes
from deal_radar.models import Listing
from deal_radar.sources.bazos import matches_profile


PRAGUE_TZ = ZoneInfo("Europe/Prague")
AD_PATH_RE = re.compile(r"/inzerat/([A-Za-z0-9_-]{5,})/([^?#]+)")
PRICE_RE = re.compile(r"(?<!\d)(\d[\d .\u00a0]*)\s*Kč", re.IGNORECASE)
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
    image_url: str | None = None

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
                self.current = _Card(
                    href=urljoin(self.base_url, href),
                    external_id=match.group(1),
                    slug=unquote(match.group(2)).strip("/"),
                    link_title=str(attributes.get("title") or attributes.get("aria-label") or ""),
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
    match = PRICE_RE.search(text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


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
        promoted = bool(re.search(r"(?:^|\n)\s*TOP\b", text, re.IGNORECASE))
        if promoted and published_at is None and not profile.include_promoted:
            continue
        title = _fallback_title(card)
        if not title:
            continue
        description = _clean_text(card.description_parts) or text
        listing = Listing(
            source="cyklobazar",
            external_id=card.external_id,
            title=title,
            description=description[:2000],
            url=card.href,
            profile=profile.name,
            price_czk=_parse_price(text),
            location=_location(card, profile.location_label),
            published_at=published_at,
            image_url=card.image_url,
        )
        if matches_profile(listing, profile):
            listings.append(listing)
    return listings


class CyklobazarSource:
    source_name = "cyklobazar"

    def __init__(self, profile: CyklobazarProfile, timeout: int = 30) -> None:
        self.profile = profile
        self.timeout = timeout

    @property
    def label(self) -> str:
        return self.profile.name

    def fetch(self) -> list[Listing]:
        try:
            html_data = get_bytes(
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
        return parse_html(html_data, self.profile)
