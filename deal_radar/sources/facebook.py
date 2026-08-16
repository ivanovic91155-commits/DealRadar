"""Facebook Marketplace через ScrapeCreators API.

Площадка не отдаёт ни RSS, ни разбираемого HTML, поэтому вместо парсера здесь
платный поисковый endpoint: один запрос — один credit. Адаптер отвечает ровно
за то же, что и остальные, — вернуть ``list[Listing]``; дедупликацией, хранением
и отправкой по-прежнему занимается сервис.

Два отличия от чешских источников, из которых растёт почти вся здешняя логика:

* **Цена приходит строкой.** В ответе есть ``amount`` и строка вида "12 000 Kč"
  или "$300", но нет кода валюты. Валюта определяется по строке, а не
  назначается: записать доллары как кроны — это ошибка в расчёте выгоды, а не
  косметика.
* **Каждый запрос стоит денег.** Отсюда потолок страниц, повторы только для
  временных ошибок и отсутствие detail-запросов по умолчанию.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from deal_radar.config import FacebookMarketplaceConfig, FacebookMarketplaceProfile
from deal_radar.http import HttpError, get_json
from deal_radar.models import Listing


LOGGER = logging.getLogger(__name__)
SOURCE_NAME = "facebook_marketplace"

# Статусы, которые повторять бессмысленно: ключ неверен, кредиты кончились,
# доступа нет. Повтор здесь только жжёт время цикла.
FATAL_STATUS_CODES = frozenset({400, 401, 402, 403, 404})

# Символ валюты в строке цены. Кода валюты в ответе нет, поэтому читаем то,
# что видит человек: "12 000 Kč", "$300", "250 €".
CURRENCY_MARKERS: tuple[tuple[str, str], ...] = (
    ("kč", "CZK"),
    ("kc", "CZK"),
    ("czk", "CZK"),
    ("€", "EUR"),
    ("eur", "EUR"),
    ("$", "USD"),
    ("usd", "USD"),
    ("£", "GBP"),
    ("gbp", "GBP"),
    ("zł", "PLN"),
    ("zl", "PLN"),
    ("pln", "PLN"),
    ("ft", "HUF"),
    ("huf", "HUF"),
)
DIGITS_RE = re.compile(r"\d[\d\s .,]*")


@dataclass(slots=True)
class ParsedPrice:
    amount: int | None = None
    currency: str = ""
    raw_text: str = ""
    status: str = "missing"
    origin: str = "not_found"


@dataclass(slots=True)
class FacebookFetchStats:
    """Что стоил цикл и что он принёс. Пишется в лог без ключа."""

    profile: str = ""
    pages: int = 0
    listings: int = 0
    kept: int = 0
    credits_charged: float = 0.0
    credits_remaining: float | None = None
    duration_ms: int = 0
    status: str = "ok"


def _text(value: Any, limit: int = 500) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _digits_to_int(text: str) -> int | None:
    """Число из строки цены. Разделители разрядов у площадки разные."""

    match = DIGITS_RE.search(text)
    if not match:
        return None
    body = match.group(0)
    # Отбрасываем дробную часть: "1 599,00" и "1,599.00" — это одна и та же
    # цена, а копейки в расчёте выгоды не участвуют.
    body = re.sub(r"[.,]\d{1,2}$", "", body.strip())
    digits = re.sub(r"\D", "", body)
    return int(digits) if digits else None


def detect_currency(formatted: str, fallback: str) -> str:
    lowered = formatted.casefold()
    for marker, code in CURRENCY_MARKERS:
        if marker in lowered:
            return code
    return fallback.upper()


def parse_price(payload: Any, profile: FacebookMarketplaceProfile) -> ParsedPrice:
    """Цена карточки: сумма, валюта и то, откуда они взялись."""

    if not isinstance(payload, dict):
        return ParsedPrice(raw_text="", status="missing", origin="not_found")
    formatted = _text(payload.get("formatted_amount"), 60)
    amount = payload.get("amount")
    number: int | None = None
    origin = "not_found"
    if isinstance(amount, (int, float)) and not isinstance(amount, bool):
        number = int(amount)
        origin = "amount"
    elif isinstance(amount, str) and amount.strip():
        number = _digits_to_int(amount)
        origin = "amount"
    # ``amount_with_offset_in_currency`` намеренно не используется: у карточки
    # с ценой "CZK3,000" и ``amount`` = 3000 это поле равно 14271, то есть цена
    # переведена в другую валюту, а не выражена в сотых долях. Как источник цены
    # оно превратило бы велосипед за 3 000 Kč в велосипед за 142 Kč.
    if number is None and formatted:
        number = _digits_to_int(formatted)
        origin = "formatted_amount"
    currency = detect_currency(formatted, profile.expected_currency)
    if number is None:
        return ParsedPrice(currency=currency, raw_text=formatted, status="missing", origin="not_found")
    if number <= 0:
        return ParsedPrice(
            amount=None, currency=currency, raw_text=formatted, status="free", origin=origin
        )
    return ParsedPrice(
        amount=number, currency=currency, raw_text=formatted, status="numeric", origin=origin
    )


def _location(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    display = _text(payload.get("display_name"), 120)
    if display:
        return display
    city = _text(payload.get("city"), 60)
    state = _text(payload.get("state"), 60)
    combined = ", ".join(part for part in (city, state) if part)
    return combined[:120] or None


def _image(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    url = _text(payload.get("uri") or payload.get("url"), 500)
    return url if url.startswith("https://") else None


def _published_at(item: dict[str, Any]) -> datetime | None:
    """Дата публикации, если площадка её вообще прислала.

    В поиске её обычно нет: выдача ограничивается фильтром ``date_listed``, а
    точного времени карточка не несёт. Выдумывать «сейчас» нельзя — на этом
    поле держится бонус за свежесть.
    """

    for key in ("creation_time", "created_at", "listing_time", "creation_timestamp"):
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return datetime.fromtimestamp(float(value), tz=UTC)
        if isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _matches(listing: Listing, profile: FacebookMarketplaceProfile) -> bool:
    """Профильные фильтры поверх серверных.

    Ценовые границы применяются только к своей валюте: сравнивать 250 EUR с
    порогом в кронах нельзя, а выбрасывать такое объявление молча — тем более.
    """

    haystack = f"{listing.title}\n{listing.description}".casefold()
    title = listing.title.casefold()
    required = [item.casefold() for item in profile.require_any_keywords if item]
    excluded = [item.casefold() for item in profile.exclude_title_keywords if item]
    if required and not any(keyword in haystack for keyword in required):
        return False
    if excluded and any(keyword in title for keyword in excluded):
        return False
    same_currency = (listing.currency or "").upper() == profile.expected_currency.upper()
    if same_currency and listing.price_amount is not None:
        if profile.min_price is not None and listing.price_amount < profile.min_price:
            return False
        if profile.max_price is not None and listing.price_amount > profile.max_price:
            return False
    return True


def parse_listing(
    item: Any,
    profile: FacebookMarketplaceProfile,
) -> Listing | None:
    """Одна карточка ответа -> ``Listing``. Кривая карточка не роняет выдачу."""

    if not isinstance(item, dict):
        return None
    external_id = _text(item.get("id"), 64)
    title = _text(item.get("title"), 300)
    url = _text(item.get("url") or item.get("listing_url"), 500)
    if not external_id or not title or not url.startswith("https://"):
        return None
    if item.get("is_sold") is True or item.get("is_hidden") is True:
        return None
    price = parse_price(item.get("price"), profile)
    listing = Listing(
        source=SOURCE_NAME,
        external_id=external_id,
        title=title,
        # Поиск описание не отдаёт: в карточке только заголовок, цена и фото.
        description=_text(item.get("description"), 2000),
        url=url,
        profile=profile.name,
        location=_location(item.get("location")),
        published_at=_published_at(item),
        image_url=_image(item.get("primary_photo") or item.get("primary_listing_photo")),
        price_amount=price.amount,
        currency=price.currency or profile.expected_currency.upper(),
        raw_price_text=price.raw_text,
        price_status=price.status,
        price_origin=price.origin,
    )
    # price_czk заполняется только для крон. Для остальных валют графа остаётся
    # пустой, и расчёт выгоды честно уходит в ручную проверку вместо того,
    # чтобы посчитать доллары кронами.
    if listing.currency == "CZK":
        listing.price_czk = price.amount
    if listing.image_url:
        listing.image_urls = [listing.image_url]
    return listing


def _photo_urls(payload: Any, limit: int) -> list[str]:
    """Ссылки на все фотографии объявления, в порядке площадки."""

    if not isinstance(payload, list):
        return []
    urls: list[str] = []
    for entry in payload:
        url = _image(entry) if isinstance(entry, dict) else None
        if url and url not in urls:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def apply_detail(listing: Listing, payload: Any, config: FacebookMarketplaceConfig) -> Listing:
    """Дополнить объявление данными со страницы: описание, фото, валюта, дата.

    Поиск отдаёт только заголовок, цену и одну картинку — по ним модель
    велосипеда почти не опознаётся. Здесь появляется всё остальное. Каждое
    поле переписывается только когда оно действительно пришло: пустой ответ
    не должен стирать то, что уже было.
    """

    if not isinstance(payload, dict):
        listing.detail_status = "failed"
        return listing
    item = payload.get("item") if isinstance(payload.get("item"), dict) else payload

    description = _text(item.get("description"), 4000)
    if description:
        listing.description = description

    photos = _photo_urls(item.get("photos"), config.max_photos_per_listing)
    if photos:
        listing.image_urls = photos
        # Обложка карточки Telegram — первая фотография объявления.
        listing.image_url = photos[0]
    elif listing.image_url:
        listing.image_urls = [listing.image_url]

    published = _published_at(item)
    if published is not None:
        listing.published_at = published

    location = _text(item.get("location_text"), 120)
    if location:
        listing.location = location

    price = item.get("price")
    if isinstance(price, dict):
        # У страницы объявления код валюты приходит явно — это надёжнее, чем
        # угадывать символ из строки вроде "CZK3,000".
        currency = _text(price.get("currency"), 3).upper()
        amount = price.get("amount")
        if currency:
            listing.currency = currency
        if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0:
            listing.price_amount = int(amount)
            listing.price_status = "numeric"
            listing.price_origin = "detail_amount"
        listing.price_czk = listing.price_amount if listing.currency == "CZK" else None

    # Состояние из атрибутов площадки дописывается к описанию: детерминированный
    # разбор состояния читает именно текст.
    attributes = item.get("attributes")
    if isinstance(attributes, list):
        labels = [
            f"{_text(entry.get('attribute_name'), 40)}: {_text(entry.get('label'), 60)}"
            for entry in attributes
            if isinstance(entry, dict) and _text(entry.get("label"), 60)
        ]
        if labels:
            listing.description = f"{listing.description}\n{' · '.join(labels)}".strip()[:4000]

    listing.detail_status = "detail"
    return listing


class FacebookMarketplaceSource:
    """Один профиль поиска. Контракт тот же, что у Bazoš и Cyklobazar."""

    source_name = SOURCE_NAME

    def __init__(
        self,
        profile: FacebookMarketplaceProfile,
        config: FacebookMarketplaceConfig,
        *,
        transport: Callable[..., dict[str, Any]] | None = None,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.profile = profile
        self.config = config
        self._transport = transport or get_json
        self._sleep = sleeper or time.sleep
        self._clock = clock or time.monotonic
        # Ноль в прошлом для любых монотонных часов: первый цикл опрашивает
        # площадку сразу, а не через час после старта.
        self._next_allowed_at = 0.0
        self.last_stats = FacebookFetchStats(profile=profile.name)

    @property
    def label(self) -> str:
        return f"facebook:{self.profile.name}"

    def _params(self, cursor: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "query": self.profile.query,
            "lat": self.profile.lat,
            "lng": self.profile.lng,
            "radius_km": self.profile.radius_km,
            "count": self.config.results_per_page,
            "availability": "available",
            "date_listed": self.config.date_listed,
            "sort_by": "creation_time_descend",
        }
        if self.profile.min_price is not None:
            params["min_price"] = self.profile.min_price
        if self.profile.max_price is not None:
            params["max_price"] = self.profile.max_price
        if self.profile.condition:
            params["condition"] = self.profile.condition
        if cursor:
            params["cursor"] = cursor
        return params

    def _request(self, cursor: str | None) -> dict[str, Any]:
        """Один поисковый запрос с ограниченными повторами.

        Повторяются только временные отказы. 401/402/403 повторять нельзя:
        ключ и остаток кредитов от повтора не изменятся.
        """

        attempt = 0
        while True:
            try:
                return self._transport(
                    f"{self.config.base_url}/search",
                    params=self._params(cursor),
                    headers={"x-api-key": self.config.api_key},
                    timeout=self.config.timeout_seconds,
                )
            except HttpError as exc:
                fatal = exc.status_code in FATAL_STATUS_CODES
                if fatal or attempt >= self.config.max_retries:
                    raise
                attempt += 1
                delay = min(8.0, 2.0**attempt)
                LOGGER.warning(
                    "Facebook profile %s request failed (%s); retry %d of %d in %.0fs",
                    self.profile.name,
                    exc.status_code or "network",
                    attempt,
                    self.config.max_retries,
                    delay,
                )
                self._sleep(delay)

    def due(self) -> bool:
        """Пора ли платить за следующий поиск.

        Общий цикл проекта короче часа, а каждый запрос стоит credit. Без
        собственного интервала один профиль потратил бы весь запас за месяц,
        поэтому лишние циклы источник пропускает молча и бесплатно.
        """

        return self._clock() >= self._next_allowed_at

    def fetch(self) -> list[Listing]:
        if not self.due():
            LOGGER.debug(
                "Facebook profile %s skipped: next search in %.0fs",
                self.profile.name,
                self._next_allowed_at - self._clock(),
            )
            return []
        self._next_allowed_at = self._clock() + self.config.min_interval_seconds
        started = time.monotonic()
        stats = FacebookFetchStats(profile=self.profile.name)
        collected: dict[str, Listing] = {}
        cursor: str | None = None
        try:
            for page in range(self.config.max_pages_per_profile):
                payload = self._request(cursor)
                stats.pages = page + 1
                stats.credits_charged += float(payload.get("credits_charged") or 0)
                remaining = payload.get("credits_remaining")
                if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
                    stats.credits_remaining = float(remaining)
                items = payload.get("listings")
                if not isinstance(items, list):
                    raise HttpError("Facebook Marketplace response has no listings array")
                stats.listings += len(items)
                for item in items:
                    listing = parse_listing(item, self.profile)
                    # Один и тот же ID внутри профиля отдаётся один раз; между
                    # профилями и циклами дедупликацию делает сервис.
                    if listing is not None and _matches(listing, self.profile):
                        collected.setdefault(listing.external_id, listing)
                cursor = payload.get("cursor") if payload.get("has_next_page") else None
                if not cursor:
                    break
        except HttpError as exc:
            stats.status = f"error:{exc.status_code or 'network'}"
            stats.duration_ms = int((time.monotonic() - started) * 1000)
            self.last_stats = stats
            self._log(stats)
            raise
        stats.kept = len(collected)
        stats.duration_ms = int((time.monotonic() - started) * 1000)
        self.last_stats = stats
        self._log(stats)
        return list(collected.values())

    def fetch_detail(self, listing: Listing) -> Listing:
        """Догрузить страницу одного объявления. Стоит ещё один credit.

        Ошибка не должна стоить объявления: карточка остаётся такой, какой её
        отдал поиск, и просто помечается как необогащённая.
        """

        try:
            payload = self._transport(
                f"{self.config.base_url}/item",
                params={"id": listing.external_id},
                headers={"x-api-key": self.config.api_key},
                timeout=self.config.timeout_seconds,
            )
        except HttpError as exc:
            LOGGER.warning(
                "Facebook detail failed for %s (%s); the listing keeps its search data",
                listing.key,
                exc.status_code or "network",
            )
            listing.detail_status = "failed"
            return listing
        enriched = apply_detail(listing, payload, self.config)
        LOGGER.info(
            "FACEBOOK_DETAIL listing=%s photos=%d description_chars=%d currency=%s "
            "credits_charged=%s credits_remaining=%s",
            listing.key,
            len(enriched.image_urls),
            len(enriched.description),
            enriched.currency,
            payload.get("credits_charged") if isinstance(payload, dict) else "?",
            payload.get("credits_remaining") if isinstance(payload, dict) else "?",
        )
        return enriched

    def _log(self, stats: FacebookFetchStats) -> None:
        LOGGER.info(
            "FACEBOOK profile=%s status=%s pages=%d listings=%d kept=%d "
            "credits_charged=%.0f credits_remaining=%s duration_ms=%d",
            stats.profile,
            stats.status,
            stats.pages,
            stats.listings,
            stats.kept,
            stats.credits_charged,
            "unknown" if stats.credits_remaining is None else f"{stats.credits_remaining:.0f}",
            stats.duration_ms,
        )
