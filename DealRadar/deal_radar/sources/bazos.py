from __future__ import annotations

import hashlib
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

from deal_radar.config import SearchProfile
from deal_radar.http import get_bytes
from deal_radar.models import Listing


# Horizontal whitespace only: a model year on the previous line must never be
# joined with the actual price (for example, "2023\n12 500 Kč").
PRICE_RE = re.compile(r"(?<!\d)(\d[\d \t\u00a0.]*)[ \t\u00a0]*Kč", re.IGNORECASE)
TITLE_PRICE_RE = re.compile(r":\s*(\d[\d \t\u00a0.]*)\s*$")
POSTCODE_RE = re.compile(r"\b\d{3}\s?\d{2}\b")
ID_RE = re.compile(r"/inzerat/(\d+)(?:/|$)")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


class _SnippetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.image_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag.lower() == "img" and not self.image_url and attributes.get("src"):
            self.image_url = attributes["src"]
        if tag.lower() in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


def _text_for(item: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in item:
        if _local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        return parsed if parsed.tzinfo else parsed.astimezone()
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.astimezone()
        except ValueError:
            return None


def _parse_price(text: str) -> int | None:
    match = PRICE_RE.search(text)
    if not match:
        # Current Bazoš RSS titles end with the listing price, for example
        # "Prodám kolo: 9 000", while the description often omits "Kč".
        title = text.splitlines()[0] if text else ""
        match = TITLE_PRICE_RE.search(title)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def _parse_location(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if POSTCODE_RE.search(line):
            candidate = line
            if index > 0 and len(lines[index - 1]) <= 80 and not PRICE_RE.search(lines[index - 1]):
                candidate = f"{lines[index - 1]}, {line}"
            return candidate[:120]
    return None


def _extract_image(item: ET.Element, snippet_image: str | None, page_url: str) -> str | None:
    if snippet_image:
        return urljoin(page_url, snippet_image)
    for child in item.iter():
        name = _local_name(child.tag)
        url = child.attrib.get("url")
        mime = child.attrib.get("type", "")
        if url and (name in {"content", "thumbnail"} or mime.startswith("image/")):
            return urljoin(page_url, url)
    return None


def parse_rss(xml_data: bytes, profile: SearchProfile) -> list[Listing]:
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        preview = xml_data[:200].decode("utf-8", errors="replace")
        raise ValueError(f"Bazoš response is not valid RSS/XML: {preview!r}") from exc

    listings: list[Listing] = []
    for item in (node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}):
        title = _text_for(item, "title")
        url = _text_for(item, "link")
        if not url:
            for child in item:
                if _local_name(child.tag) == "link" and child.attrib.get("href"):
                    url = child.attrib["href"]
                    break
        guid = _text_for(item, "guid", "id")
        raw_description = _text_for(item, "description", "summary", "content")
        parser = _SnippetParser()
        parser.feed(raw_description)
        description = parser.text()
        combined = "\n".join(part for part in (title, description) if part)
        id_match = ID_RE.search(url)
        external_id = id_match.group(1) if id_match else ""
        if not external_id and guid:
            digits = re.search(r"(?:^|\D)(\d{6,})(?:\D|$)", guid)
            external_id = digits.group(1) if digits else ""
        if not external_id:
            external_id = hashlib.sha256((url or title).encode("utf-8")).hexdigest()[:20]
        published_raw = _text_for(item, "pubdate", "published", "updated", "date")
        if not title or not url:
            continue
        listings.append(
            Listing(
                source="bazos",
                external_id=external_id,
                title=title,
                description=description,
                url=url,
                profile=profile.name,
                price_czk=_parse_price(combined),
                location=_parse_location(description),
                published_at=_parse_datetime(published_raw),
                image_url=_extract_image(item, parser.image_url, url),
            )
        )
    return listings


def matches_profile(listing: Listing, profile: SearchProfile) -> bool:
    searchable = _normalize(f"{listing.title}\n{listing.description}")
    title = _normalize(listing.title)
    required = [_normalize(keyword) for keyword in profile.require_any_keywords]
    excluded = [_normalize(keyword) for keyword in profile.exclude_title_keywords]
    def contains_keyword(haystack: str, keyword: str) -> bool:
        return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", haystack) is not None

    if required and not any(contains_keyword(searchable, keyword) for keyword in required):
        return False
    if excluded and any(contains_keyword(title, keyword) for keyword in excluded):
        return False
    if listing.price_czk is not None:
        if profile.min_price_czk is not None and listing.price_czk < profile.min_price_czk:
            return False
        if profile.max_price_czk is not None and listing.price_czk > profile.max_price_czk:
            return False
    return True


class BazosSource:
    source_name = "bazos"

    def __init__(self, profile: SearchProfile, timeout: int = 30) -> None:
        self.profile = profile
        self.timeout = timeout

    @property
    def label(self) -> str:
        return self.profile.name

    def fetch(self) -> list[Listing]:
        xml_data = get_bytes(self.profile.rss_url, timeout=self.timeout)
        return [
            listing
            for listing in parse_rss(xml_data, self.profile)
            if matches_profile(listing, self.profile)
        ]
