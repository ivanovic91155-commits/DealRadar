from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from deal_radar.bike_identity import build_search_queries, has_used_terms, normalize_text
from deal_radar.http import get_bytes
from deal_radar.models import BikeIdentity, RetailOffer


LOGGER = logging.getLogger(__name__)
ZBOZI_BASE = "https://www.zbozi.cz"


class PriceSource(Protocol):
    name: str

    def search(self, identity: BikeIdentity) -> list[RetailOffer]: ...


class _NextDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_next_data = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.in_next_data = tag.lower() == "script" and attributes.get("id") == "__NEXT_DATA__"

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self.in_next_data = False

    def handle_data(self, data: str) -> None:
        if self.in_next_data:
            self.parts.append(data)


def extract_next_data(html_data: bytes) -> dict[str, Any]:
    parser = _NextDataParser()
    parser.feed(html_data.decode("utf-8", errors="replace"))
    if not parser.parts:
        raise ValueError("Page does not contain __NEXT_DATA__")
    value = json.loads("".join(parser.parts))
    if not isinstance(value, dict):
        raise ValueError("__NEXT_DATA__ must be a JSON object")
    return value


def _page_data(html_data: bytes) -> dict[str, Any]:
    value = extract_next_data(html_data)
    page_props = value.get("props", {}).get("pageProps", {})
    data = page_props.get("data", {})
    if not isinstance(data, dict):
        raise ValueError("Zboží page does not contain product data")
    return data


def _price_czk(value: Any) -> int | None:
    try:
        cents = int(value)
    except (TypeError, ValueError):
        return None
    return int(round(cents / 100)) if cents > 0 else None


def _offer_from_item(item: dict[str, Any], fallback_seller: str = "Zboží.cz") -> RetailOffer | None:
    price = _price_czk(item.get("price") or item.get("minPrice"))
    name = str(item.get("name") or item.get("imageAlt") or "").strip()
    # Search results sometimes expose an opaque tracking token in ``url``;
    # ``detailUrl`` is the stable exact-offer page. Product detail offers only
    # have a /clickthru URL, which resolves to the merchant product page.
    if item.get("detailUrl"):
        raw_url = str(item["detailUrl"]).strip()
    elif item.get("offerId"):
        raw_url = f"/nabidka/{item['offerId']}/"
    else:
        raw_url = str(item.get("url") or "").strip()
    shop = item.get("shop") if isinstance(item.get("shop"), dict) else {}
    seller = str(shop.get("name") or item.get("cheapOfferShopName") or fallback_seller).strip()
    if not name or not raw_url or not price:
        return None
    availability = str(item.get("availability") or "unknown").casefold()
    condition = "used" if has_used_terms(name) else "new"
    offer_url = urljoin(ZBOZI_BASE, raw_url)
    parsed_url = urlsplit(offer_url)
    if parsed_url.hostname == "www.zbozi.cz" and parsed_url.path.startswith("/nabidka/"):
        offer_url = urlunsplit((parsed_url.scheme, parsed_url.netloc, parsed_url.path, "", ""))
    return RetailOffer(
        seller=seller[:100],
        product_name=name[:300],
        price_czk=price,
        original_price=float(price),
        currency="CZK",
        url=offer_url,
        availability=availability,
        condition=condition,
    )


def _mentions_identity(name: str, identity: BikeIdentity) -> bool:
    normalized = normalize_text(name)
    brand = normalize_text(identity.brand)
    model_tokens = [token for token in normalize_text(identity.model).split() if len(token) > 1 or token.isdigit()]
    return bool(brand and brand in normalized and model_tokens and all(token in normalized.split() for token in model_tokens))


class ZboziPriceSource:
    name = "zbozi"

    def __init__(
        self,
        timeout: int = 15,
        max_queries: int = 2,
        max_product_pages: int = 3,
        max_parallel_requests: int = 3,
        fetcher: Callable[[str, int], bytes] = get_bytes,
    ) -> None:
        self.timeout = timeout
        self.max_queries = max(1, max_queries)
        self.max_product_pages = max(0, max_product_pages)
        self.max_parallel_requests = max(1, max_parallel_requests)
        self.fetcher = fetcher

    def _search_url(self, query: str) -> str:
        return f"{ZBOZI_BASE}/hledej/?{urlencode({'q': query})}"

    def _detail_offers(self, url: str) -> list[RetailOffer]:
        data = _page_data(self.fetcher(url, self.timeout))
        offers_block = data.get("offers", {})
        items = offers_block.get("items", []) if isinstance(offers_block, dict) else []
        offers: list[RetailOffer] = []
        for item in items:
            if isinstance(item, dict):
                offer = _offer_from_item(item)
                if offer:
                    offers.append(offer)
        return offers

    def search(self, identity: BikeIdentity) -> list[RetailOffer]:
        offers: list[RetailOffer] = []
        product_urls: list[str] = []
        for query in build_search_queries(identity)[: self.max_queries]:
            data = _page_data(self.fetcher(self._search_url(query), self.timeout))
            results_block = data.get("results", {})
            items = results_block.get("results", []) if isinstance(results_block, dict) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                if not _mentions_identity(name, identity):
                    continue
                if item.get("type") == "offer":
                    offer = _offer_from_item(item)
                    if offer:
                        offers.append(offer)
                elif item.get("type") == "product" and item.get("url"):
                    url = urljoin(ZBOZI_BASE, str(item["url"]))
                    if url not in product_urls:
                        product_urls.append(url)

        selected_urls = product_urls[: self.max_product_pages]
        if selected_urls:
            workers = min(self.max_parallel_requests, len(selected_urls))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="zbozi") as executor:
                futures = {executor.submit(self._detail_offers, url): url for url in selected_urls}
                for future in as_completed(futures):
                    try:
                        offers.extend(future.result())
                    except Exception as exc:
                        LOGGER.warning("Zboží product page failed (%s): %s", futures[future], exc)

        unique: dict[tuple[str, str], RetailOffer] = {}
        for offer in offers:
            unique.setdefault((offer.seller.casefold(), offer.url), offer)
        return list(unique.values())
