from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import quote_plus, urlencode, urljoin

from deal_radar.bike_identity import normalize_text
from deal_radar.config import SearchProfile
from deal_radar.http import get_bytes
from deal_radar.models import BikeIdentity, MarketComparable
from deal_radar.sources.bazos import parse_rss


class UsedMarketSource(Protocol):
    name: str
    country: str
    request_count: int

    def search(self, identity: BikeIdentity) -> list[MarketComparable]: ...


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join("".join(self.parts).split())


def _text(fragment: str) -> str:
    parser = _TextParser()
    parser.feed(fragment)
    return parser.text()


def _price(value: str) -> float | None:
    clean = html.unescape(value).replace("\u00a0", " ")
    matches = re.findall(r"\d[\d .,'\u00a0]*", clean)
    if not matches:
        return None
    raw = matches[0].strip().replace(" ", "").replace("\u00a0", "").replace("'", "")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(".", "")
    try:
        result = float(raw)
    except ValueError:
        return None
    return result if result > 0 else None


def _image_fingerprint(url: str) -> str:
    if not url:
        return ""
    normalized = re.sub(r"[?&](?:rule|width|height)=[^&]+", "", url.casefold())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class BazosCzechMarketSource:
    name = "bazos_cz"
    country = "CZ"

    def __init__(
        self,
        timeout: int = 20,
        max_results: int = 25,
        fetcher: Callable[[str, int], bytes] = get_bytes,
    ) -> None:
        self.timeout = timeout
        self.max_results = max_results
        self.fetcher = fetcher
        self.request_count = 0

    def search(self, identity: BikeIdentity) -> list[MarketComparable]:
        query = " ".join(part for part in (identity.brand, identity.model, identity.generation) if part)
        url = "https://www.bazos.cz/rss.php?" + urlencode({"hledat": query})
        self.request_count += 1
        profile = SearchProfile(name="Market Price Engine — celé Česko", rss_url=url)
        listings = parse_rss(self.fetcher(url, self.timeout), profile)
        now = datetime.now(UTC).isoformat()
        return [
            MarketComparable(
                source="bazos",
                source_listing_id=item.external_id,
                url=item.url,
                country="CZ",
                title=item.title,
                description=item.description,
                price_original=float(item.price_czk) if item.price_czk else None,
                currency="CZK",
                price_czk=item.price_czk,
                published_at=item.published_at.isoformat() if item.published_at else "",
                first_seen_at=now,
                last_seen_at=now,
                image_url=item.image_url or "",
                image_fingerprint=_image_fingerprint(item.image_url or ""),
            )
            for item in listings[: self.max_results]
            if item.price_czk
        ]


class KleinanzeigenMarketSource:
    name = "kleinanzeigen_de"
    country = "DE"
    base_url = "https://www.kleinanzeigen.de"

    def __init__(
        self,
        timeout: int = 20,
        max_results: int = 25,
        fetcher: Callable[[str, int], bytes] = get_bytes,
    ) -> None:
        self.timeout = timeout
        self.max_results = max_results
        self.fetcher = fetcher
        self.request_count = 0

    @classmethod
    def parse(cls, html_data: bytes, max_results: int = 25) -> list[MarketComparable]:
        page = html_data.decode("utf-8", errors="replace")
        results: list[MarketComparable] = []
        for match in re.finditer(r'<article class="aditem"(?P<body>.*?)</article>', page, re.DOTALL):
            chunk = match.group(0)
            id_match = re.search(r'data-adid="(\d+)"', chunk)
            href_match = re.search(r'data-href="([^"]+)"', chunk)
            price_match = re.search(
                r'aditem-main--middle--price-shipping--price[^>]*>(.*?)</p>', chunk, re.DOTALL
            )
            script_match = re.search(
                r'<script type="application/ld\+json">(.*?)</script>', chunk, re.DOTALL
            )
            if not id_match or not href_match or not price_match:
                continue
            data: dict[str, object] = {}
            if script_match:
                try:
                    parsed = json.loads(script_match.group(1))
                    if isinstance(parsed, dict):
                        data = parsed
                except json.JSONDecodeError:
                    pass
            title_match = re.search(r'<a class="ellipsis".*?>(.*?)</a>', chunk, re.DOTALL)
            description_match = re.search(
                r'aditem-main--middle--description[^>]*>(.*?)</p>', chunk, re.DOTALL
            )
            title = str(data.get("title") or (_text(title_match.group(1)) if title_match else ""))
            description = str(
                data.get("description")
                or (_text(description_match.group(1)) if description_match else "")
            )
            price = _price(_text(price_match.group(1)))
            if not title or not price:
                continue
            image_url = str(data.get("contentUrl") or "")
            location_match = re.search(r'aditem-main--top--left[^>]*>(.*?)</div>', chunk, re.DOTALL)
            results.append(
                MarketComparable(
                    source=cls.name,
                    source_listing_id=id_match.group(1),
                    url=urljoin(cls.base_url, html.unescape(href_match.group(1))),
                    country="DE",
                    title=title,
                    description=description,
                    price_original=price,
                    currency="EUR",
                    location=_text(location_match.group(1)) if location_match else "",
                    image_url=image_url,
                    image_fingerprint=_image_fingerprint(image_url),
                )
            )
            if len(results) >= max_results:
                break
        return results

    def search(self, identity: BikeIdentity) -> list[MarketComparable]:
        query = " ".join(part for part in (identity.brand, identity.model, identity.generation) if part)
        slug = quote_plus(query).replace("+", "-").casefold()
        url = f"{self.base_url}/s-fahrraeder/{slug}/k0c217"
        self.request_count += 1
        return self.parse(self.fetcher(url, self.timeout), self.max_results)


class MarktplaatsMarketSource:
    name = "marktplaats_nl"
    country = "NL"
    base_url = "https://www.marktplaats.nl"

    def __init__(
        self,
        timeout: int = 20,
        max_results: int = 25,
        fetcher: Callable[[str, int], bytes] = get_bytes,
    ) -> None:
        self.timeout = timeout
        self.max_results = max_results
        self.fetcher = fetcher
        self.request_count = 0

    @classmethod
    def parse(cls, html_data: bytes, max_results: int = 25) -> list[MarketComparable]:
        page = html_data.decode("utf-8", errors="replace")
        starts = [match.start() for match in re.finditer(r'<li class="hz-Listing\b', page)]
        results: list[MarketComparable] = []
        seen: set[str] = set()
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else min(len(page), start + 80000)
            chunk = page[start:end]
            href_match = re.search(r'href="(/v/[^"?]+/m(\d+)-[^"]+)"', chunk)
            title_match = re.search(r'ListingTitle_[^>]*>(.*?)</span>', chunk, re.DOTALL)
            description_match = re.search(r'ListingDescription_[^>]*>(.*?)</span>', chunk, re.DOTALL)
            price_match = re.search(r'ListingPrice_[^>]*>(.*?)</h5>', chunk, re.DOTALL)
            if not href_match or not title_match or not price_match or href_match.group(2) in seen:
                continue
            price = _price(_text(price_match.group(1)))
            title = _text(title_match.group(1))
            if not title or not price:
                continue
            seen.add(href_match.group(2))
            seller_match = re.search(r'hz-Listing-seller-name-new[^>]*>(.*?)</span>', chunk, re.DOTALL)
            location_match = re.search(r'data-testid="location-label"[^>]*>(.*?)</span>', chunk, re.DOTALL)
            image_match = re.search(r'<img[^>]+(?:data-src|src)="([^"]+)"', chunk)
            image_url = html.unescape(image_match.group(1)) if image_match else ""
            results.append(
                MarketComparable(
                    source=cls.name,
                    source_listing_id=href_match.group(2),
                    url=urljoin(cls.base_url, html.unescape(href_match.group(1))),
                    country="NL",
                    title=title,
                    description=_text(description_match.group(1)) if description_match else "",
                    price_original=price,
                    currency="EUR",
                    seller=_text(seller_match.group(1)) if seller_match else "",
                    location=_text(location_match.group(1)) if location_match else "",
                    image_url=image_url,
                    image_fingerprint=_image_fingerprint(image_url),
                )
            )
            if len(results) >= max_results:
                break
        return results

    def search(self, identity: BikeIdentity) -> list[MarketComparable]:
        query = " ".join(part for part in (identity.brand, identity.model, identity.generation) if part)
        url = f"{self.base_url}/l/fietsen-en-brommers/q/{quote_plus(query)}/"
        self.request_count += 1
        return self.parse(self.fetcher(url, self.timeout), self.max_results)


class BuycycleMarketSource:
    name = "buycycle_eu"
    country = "EU"
    base_url = "https://buycycle.com"

    def __init__(
        self,
        timeout: int = 20,
        max_results: int = 25,
        fetcher: Callable[[str, int], bytes] = get_bytes,
    ) -> None:
        self.timeout = timeout
        self.max_results = max_results
        self.fetcher = fetcher
        self.request_count = 0

    @classmethod
    def parse(cls, html_data: bytes, max_results: int = 25) -> list[MarketComparable]:
        page = html_data.decode("utf-8", errors="replace")
        pattern = re.compile(
            r'data-cnstrc-item-id="(?P<id>[^"]+)"\s+'
            r'data-cnstrc-item-name="(?P<name>[^"]+)"\s+'
            r'data-cnstrc-item-price="(?P<price>[^"]+)"'
        )
        matches = list(pattern.finditer(page))
        results: list[MarketComparable] = []
        for index, match in enumerate(matches[:max_results]):
            end = matches[index + 1].start() if index + 1 < len(matches) else min(len(page), match.end() + 12000)
            chunk = page[match.end() : end]
            href_match = re.search(r'<a[^>]+href="([^"]+/product/[^"]+)"', chunk)
            if not href_match:
                continue
            price = _price(match.group("price"))
            if not price:
                continue
            image_match = re.search(r'<img[^>]+src="(https://d1mgeijqpfaspl[^"?]+[^\"]*)"', chunk)
            image_url = html.unescape(image_match.group(1)) if image_match else ""
            results.append(
                MarketComparable(
                    source=cls.name,
                    source_listing_id=match.group("id"),
                    url=urljoin(cls.base_url, html.unescape(href_match.group(1))),
                    country="EU",
                    title=html.unescape(match.group("name")),
                    description=_text(chunk)[:500],
                    price_original=price,
                    currency="EUR",
                    image_url=image_url,
                    image_fingerprint=_image_fingerprint(image_url),
                )
            )
        return results

    def search(self, identity: BikeIdentity) -> list[MarketComparable]:
        query = " ".join(part for part in (identity.brand, identity.model, identity.generation) if part)
        url = f"{self.base_url}/en-us/shop/search/{quote_plus(query)}"
        self.request_count += 1
        items = self.parse(self.fetcher(url, self.timeout), self.max_results)
        brand = identity.brand.strip()
        if brand:
            for item in items:
                if normalize_text(brand) not in normalize_text(item.title).split():
                    item.title = f"{brand} {item.title}"
        return items
