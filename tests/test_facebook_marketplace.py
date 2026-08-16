"""Facebook Marketplace через ScrapeCreators.

Ни один тест не ходит в сеть и не тратит credits: транспорт всегда подменяется.
Ключ в тестах фиктивный и в утверждениях не участвует.
"""

from __future__ import annotations

import json
import logging
import os
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from deal_radar.config import (
    AppConfig,
    DealScoringConfig,
    FacebookMarketplaceConfig,
    FacebookMarketplaceProfile,
    MarketPricingConfig,
    RetailConfig,
    SearchProfile,
    TelegramConfig,
    TelegramGateConfig,
    load_config,
)
from deal_radar.http import HttpError
from deal_radar.models import Listing
from deal_radar.service import DealRadarService
from deal_radar.sources.facebook import (
    FacebookMarketplaceSource,
    detect_currency,
    parse_listing,
    parse_price,
)
from deal_radar.telegram import format_seller_price

FAKE_KEY = "test-key-not-a-real-credential"


def profile(**overrides: Any) -> FacebookMarketplaceProfile:
    settings: dict[str, Any] = {
        "name": "Praha kola",
        "query": "kolo",
        "lat": 50.0755,
        "lng": 14.4378,
        "radius_km": 60,
        "expected_currency": "CZK",
    }
    settings.update(overrides)
    return FacebookMarketplaceProfile(**settings)


def config(**overrides: Any) -> FacebookMarketplaceConfig:
    settings: dict[str, Any] = {
        "enabled": True,
        "api_key": FAKE_KEY,
        "profiles": [profile()],
    }
    settings.update(overrides)
    return FacebookMarketplaceConfig(**settings)


def card(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "1122334455",
        "url": "https://www.facebook.com/marketplace/item/1122334455/",
        "title": "Trek Marlin 7 2024",
        "price": {
            "formatted_amount": "14 900 Kč",
            "amount": 14900,
            # Площадка кладёт сюда пересчёт в другую валюту, а не сотые доли.
            "amount_with_offset_in_currency": 70873,
        },
        "location": {"city": "Praha", "state": "Praha", "display_name": "Praha, Praha"},
        "primary_photo": {"id": "9", "url": "https://scontent.example/photo.jpg"},
        "is_sold": False,
        "is_live": True,
    }
    base.update(overrides)
    return base


def response(*cards: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": True,
        "credits_charged": 1,
        "credits_remaining": 6099,
        "listings": list(cards) or [card()],
        "cursor": None,
        "has_next_page": False,
    }
    payload.update(overrides)
    return payload


class RecordingTransport:
    """Подменённый HTTP: записывает запросы, отдаёт заготовленные ответы."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses) or [response()]
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, params=None, headers=None, timeout=30) -> dict[str, Any]:
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}, "timeout": timeout})
        item = self.responses[min(len(self.calls), len(self.responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item


def source(transport: RecordingTransport, **config_overrides: Any) -> FacebookMarketplaceSource:
    settings = config(**config_overrides)
    return FacebookMarketplaceSource(
        settings.profiles[0], settings, transport=transport, sleeper=lambda _: None
    )


class RequestTest(unittest.TestCase):
    def test_search_parameters_follow_the_documented_contract(self) -> None:
        transport = RecordingTransport()
        source(transport, profiles=[profile(min_price=1000, max_price=100000, condition="used_good")]).fetch()
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://api.scrapecreators.com/v1/facebook/marketplace/search")
        self.assertEqual(
            call["params"],
            {
                "query": "kolo",
                "lat": 50.0755,
                "lng": 14.4378,
                "radius_km": 60,
                "count": 24,
                "availability": "available",
                "date_listed": "last_24_hours",
                "sort_by": "creation_time_descend",
                "min_price": 1000,
                "max_price": 100000,
                "condition": "used_good",
            },
        )
        self.assertEqual(call["headers"], {"x-api-key": FAKE_KEY})

    def test_one_active_profile_costs_one_credit_per_cycle(self) -> None:
        transport = RecordingTransport()
        instance = source(transport)
        instance.fetch()
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(instance.last_stats.credits_charged, 1)
        self.assertEqual(instance.last_stats.credits_remaining, 6099)

    def test_pagination_stops_at_the_configured_page_limit(self) -> None:
        pages = [
            response(card(id="a"), cursor="c1", has_next_page=True),
            response(card(id="b"), cursor="c2", has_next_page=True),
            response(card(id="c"), cursor=None, has_next_page=False),
        ]
        transport = RecordingTransport(*pages)
        listings = source(transport, max_pages_per_profile=2).fetch()
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[1]["params"]["cursor"], "c1")
        self.assertEqual({item.external_id for item in listings}, {"a", "b"})

    def test_pagination_stops_when_the_api_says_there_is_no_next_page(self) -> None:
        transport = RecordingTransport(response(card(id="only"), has_next_page=False, cursor="ignored"))
        source(transport, max_pages_per_profile=5).fetch()
        self.assertEqual(len(transport.calls), 1)

    def test_the_source_is_not_queried_more_than_once_per_interval(self) -> None:
        # Общий цикл проекта — 420 секунд. Без собственного интервала один
        # профиль потратил бы 6171 credit в месяц, то есть весь запас.
        now = [1000.0]
        settings = config()
        instance = FacebookMarketplaceSource(
            settings.profiles[0],
            settings,
            transport=(transport := RecordingTransport(response(), response())),
            sleeper=lambda _: None,
            clock=lambda: now[0],
        )
        self.assertEqual(len(instance.fetch()), 1)
        now[0] += 420  # следующий общий цикл
        self.assertEqual(instance.fetch(), [])
        now[0] += 1200  # прошло 20 минут
        self.assertEqual(len(instance.fetch()), 1)
        self.assertEqual(len(transport.calls), 2)

    def test_the_interval_keeps_one_profile_inside_the_budget(self) -> None:
        settings = config()
        self.assertEqual(settings.min_interval_seconds, 1200)
        self.assertEqual(settings.credits_per_month(), 2160)
        self.assertLess(settings.credits_per_month(), 6100)

    def test_detail_requests_are_off_by_default(self) -> None:
        self.assertEqual(FacebookMarketplaceConfig().max_details_per_run, 0)
        transport = RecordingTransport()
        source(transport).fetch()
        self.assertTrue(all(call["url"].endswith("/search") for call in transport.calls))


class NormalisationTest(unittest.TestCase):
    def test_a_czech_card_becomes_a_complete_listing(self) -> None:
        listing = parse_listing(card(), profile())
        assert listing is not None
        self.assertEqual(listing.source, "facebook_marketplace")
        self.assertEqual(listing.external_id, "1122334455")
        self.assertEqual(listing.title, "Trek Marlin 7 2024")
        self.assertEqual(listing.description, "")
        self.assertEqual(listing.url, "https://www.facebook.com/marketplace/item/1122334455/")
        self.assertEqual(listing.profile, "Praha kola")
        self.assertEqual(listing.location, "Praha, Praha")
        self.assertEqual(listing.image_url, "https://scontent.example/photo.jpg")
        self.assertEqual(listing.price_amount, 14900)
        self.assertEqual(listing.currency, "CZK")
        self.assertEqual(listing.price_czk, 14900)
        self.assertEqual(listing.price_status, "numeric")
        self.assertIsNone(listing.published_at)

    def test_missing_fields_do_not_break_the_batch(self) -> None:
        transport = RecordingTransport(
            response(
                {"id": "no-title", "url": "https://www.facebook.com/marketplace/item/1/"},
                {"id": "no-url", "title": "Kolo"},
                "not-a-dict",
                card(id="good", location=None, primary_photo=None, price=None),
            )
        )
        listings = source(transport).fetch()
        self.assertEqual([item.external_id for item in listings], ["good"])
        survivor = listings[0]
        self.assertIsNone(survivor.location)
        self.assertIsNone(survivor.image_url)
        self.assertIsNone(survivor.price_amount)
        self.assertEqual(survivor.price_status, "missing")

    def test_sold_and_hidden_cards_are_skipped(self) -> None:
        transport = RecordingTransport(
            response(card(id="sold", is_sold=True), card(id="hidden", is_hidden=True), card(id="live"))
        )
        self.assertEqual([item.external_id for item in source(transport).fetch()], ["live"])

    def test_published_at_is_read_when_the_api_sends_it(self) -> None:
        moment = datetime(2026, 8, 16, 9, 30, tzinfo=UTC)
        epoch = parse_listing(card(creation_time=int(moment.timestamp())), profile())
        iso = parse_listing(card(created_at="2026-08-16T09:30:00Z"), profile())
        assert epoch is not None and iso is not None
        self.assertEqual(epoch.published_at, moment)
        self.assertEqual(iso.published_at, moment)

    def test_profile_keywords_filter_the_response(self) -> None:
        transport = RecordingTransport(
            response(card(id="bike", title="Horské kolo Trek"), card(id="parts", title="Kolo - náhradní díly"))
        )
        listings = source(
            transport,
            profiles=[profile(require_any_keywords=["kolo"], exclude_title_keywords=["náhradní díly"])],
        ).fetch()
        self.assertEqual([item.external_id for item in listings], ["bike"])


class CurrencyTest(unittest.TestCase):
    def test_currency_is_read_from_the_formatted_amount(self) -> None:
        for formatted, expected in (
            ("14 900 Kč", "CZK"),
            ("$300", "USD"),
            ("250 €", "EUR"),
            ("1 200 zł", "PLN"),
            ("£180", "GBP"),
        ):
            with self.subTest(formatted=formatted):
                self.assertEqual(detect_currency(formatted, "CZK"), expected)

    def test_foreign_currency_never_becomes_czk(self) -> None:
        listing = parse_listing(
            card(price={"formatted_amount": "$300", "amount": 300}), profile()
        )
        assert listing is not None
        self.assertEqual(listing.price_amount, 300)
        self.assertEqual(listing.currency, "USD")
        self.assertIsNone(listing.price_czk)
        # Ни один расчёт в кронах не должен принять доллары за кроны.
        self.assertIsNone(listing.comparable_price_czk)

    def test_euro_listing_keeps_its_currency_in_telegram(self) -> None:
        listing = parse_listing(
            card(price={"formatted_amount": "250 €", "amount": 250}), profile(expected_currency="EUR")
        )
        assert listing is not None
        self.assertEqual(format_seller_price(listing), "250 EUR")

    def test_the_profile_currency_is_only_a_fallback(self) -> None:
        # Символа в строке нет — берём ожидание профиля.
        unmarked = parse_listing(card(price={"formatted_amount": "14900", "amount": 14900}), profile())
        # Символ есть и он спорит с ожиданием — верим площадке, а не конфигу.
        marked = parse_listing(
            card(price={"formatted_amount": "$300", "amount": 300}), profile(expected_currency="CZK")
        )
        assert unmarked is not None and marked is not None
        self.assertEqual(unmarked.currency, "CZK")
        self.assertEqual(marked.currency, "USD")

    def test_price_falls_back_to_the_formatted_string(self) -> None:
        text_only = parse_price({"formatted_amount": "14 900 Kč"}, profile())
        self.assertEqual(text_only.amount, 14900)
        self.assertEqual(text_only.origin, "formatted_amount")

    def test_the_offset_field_is_never_used_as_a_price(self) -> None:
        # Реальная карточка площадки: цена "CZK3,000", amount 3000, а
        # amount_with_offset_in_currency равен 14271 — это пересчёт в другую
        # валюту, а не сотые доли. Как источник цены он сделал бы из
        # велосипеда за 3 000 Kč велосипед за 142 Kč.
        real = parse_price(
            {"formatted_amount": "CZK3,000", "amount": 3000, "amount_with_offset_in_currency": 14271},
            profile(),
        )
        self.assertEqual(real.amount, 3000)
        self.assertEqual(real.origin, "amount")
        without_amount = parse_price(
            {"formatted_amount": "CZK3,000", "amount_with_offset_in_currency": 14271}, profile()
        )
        self.assertEqual(without_amount.amount, 3000)
        self.assertEqual(without_amount.origin, "formatted_amount")

    def test_the_live_price_format_of_the_platform_is_understood(self) -> None:
        # Так площадка присылает цену на самом деле: код валюты слитно с суммой.
        parsed = parse_price({"formatted_amount": "CZK3,000", "amount": 3000}, profile())
        self.assertEqual(parsed.amount, 3000)
        self.assertEqual(parsed.currency, "CZK")

    def test_a_free_listing_is_not_a_numeric_price(self) -> None:
        parsed = parse_price({"formatted_amount": "Zdarma", "amount": 0}, profile())
        self.assertEqual(parsed.status, "free")
        self.assertIsNone(parsed.amount)


def detail_response(**overrides: Any) -> dict[str, Any]:
    """Ответ страницы объявления: описание, вся галерея, валюта, дата."""

    item: dict[str, Any] = {
        "id": "1122334455",
        "url": "https://www.facebook.com/marketplace/item/1122334455/",
        "title": "Trek Marlin 7 2024",
        "description": "Trek Marlin 7, rám 19\", kola 29\", Shimano Deore, hydraulické brzdy.",
        "creation_time": "2026-08-16T09:30:00Z",
        "location_text": "Praha 6, Czech Republic",
        "price": {"amount": 14900, "currency": "CZK", "formatted_amount_zeros_stripped": "CZK14,900"},
        "photos": [
            {"id": "1", "url": "https://scontent.example/a.jpg"},
            {"id": "2", "url": "https://scontent.example/b.jpg"},
            {"id": "3", "url": "https://scontent.example/c.jpg"},
        ],
        "attributes": [{"attribute_name": "Condition", "value": "used_good", "label": "Used - good"}],
        "is_sold": False,
        "is_live": True,
    }
    item.update(overrides)
    return {"success": True, "credits_charged": 1, "credits_remaining": 6097, **item}


class DetailEnrichmentTest(unittest.TestCase):
    """Страница объявления — единственный источник описания и всех фотографий."""

    def test_detail_fills_description_photos_currency_and_date(self) -> None:
        transport = RecordingTransport(response(), detail_response())
        instance = source(transport, detail_enabled=True, max_details_per_run=5)
        listing = instance.fetch()[0]
        self.assertEqual(listing.description, "")  # поиск описания не даёт
        enriched = instance.fetch_detail(listing)
        self.assertIn("Shimano Deore", enriched.description)
        self.assertIn("Condition: Used - good", enriched.description)
        self.assertEqual(
            enriched.image_urls,
            [
                "https://scontent.example/a.jpg",
                "https://scontent.example/b.jpg",
                "https://scontent.example/c.jpg",
            ],
        )
        self.assertEqual(enriched.image_url, "https://scontent.example/a.jpg")
        self.assertEqual(enriched.currency, "CZK")
        self.assertEqual(enriched.price_czk, 14900)
        self.assertEqual(enriched.location, "Praha 6, Czech Republic")
        self.assertEqual(enriched.published_at, datetime(2026, 8, 16, 9, 30, tzinfo=UTC))
        self.assertEqual(enriched.detail_status, "detail")

    def test_the_detail_endpoint_is_called_with_the_listing_id(self) -> None:
        transport = RecordingTransport(response(), detail_response())
        instance = source(transport, detail_enabled=True, max_details_per_run=5)
        instance.fetch_detail(instance.fetch()[0])
        call = transport.calls[1]
        self.assertTrue(call["url"].endswith("/item"))
        self.assertEqual(call["params"], {"id": "1122334455"})
        self.assertEqual(call["headers"], {"x-api-key": FAKE_KEY})

    def test_the_photo_count_is_capped(self) -> None:
        transport = RecordingTransport(response(), detail_response())
        instance = source(transport, detail_enabled=True, max_details_per_run=5, max_photos_per_listing=2)
        enriched = instance.fetch_detail(instance.fetch()[0])
        self.assertEqual(len(enriched.image_urls), 2)

    def test_a_real_currency_code_beats_the_guessed_symbol(self) -> None:
        transport = RecordingTransport(
            response(card(price={"formatted_amount": "14 900 Kč", "amount": 14900})),
            detail_response(price={"amount": 600, "currency": "EUR"}),
        )
        instance = source(transport, detail_enabled=True, max_details_per_run=5)
        enriched = instance.fetch_detail(instance.fetch()[0])
        self.assertEqual(enriched.currency, "EUR")
        self.assertEqual(enriched.price_amount, 600)
        self.assertIsNone(enriched.price_czk)

    def test_a_failed_detail_keeps_the_search_data(self) -> None:
        transport = RecordingTransport(response(), HttpError("HTTP 500", 500))
        instance = source(transport, detail_enabled=True, max_details_per_run=5)
        listing = instance.fetch()[0]
        enriched = instance.fetch_detail(listing)
        self.assertEqual(enriched.detail_status, "failed")
        self.assertEqual(enriched.title, "Trek Marlin 7 2024")
        self.assertEqual(enriched.price_amount, 14900)

    def test_an_empty_detail_does_not_erase_what_search_found(self) -> None:
        transport = RecordingTransport(response(), {"success": True, "id": "1122334455"})
        instance = source(transport, detail_enabled=True, max_details_per_run=5)
        listing = instance.fetch()[0]
        enriched = instance.fetch_detail(listing)
        self.assertEqual(enriched.title, "Trek Marlin 7 2024")
        self.assertEqual(enriched.price_amount, 14900)
        self.assertEqual(enriched.image_urls, ["https://scontent.example/photo.jpg"])

    def test_detail_is_disabled_by_default(self) -> None:
        self.assertFalse(FacebookMarketplaceConfig().detail_enabled)
        self.assertEqual(FacebookMarketplaceConfig().max_details_per_run, 0)

    def test_enabling_detail_without_a_budget_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FacebookMarketplaceConfig(
                detail_enabled=True, max_details_per_run=0, profiles=[profile()]
            ).validate()


class ErrorHandlingTest(unittest.TestCase):
    def test_authentication_and_payment_errors_are_not_retried(self) -> None:
        for status in (400, 401, 402, 403, 404):
            with self.subTest(status=status):
                transport = RecordingTransport(HttpError(f"HTTP {status}", status))
                with self.assertRaises(HttpError):
                    source(transport).fetch()
                self.assertEqual(len(transport.calls), 1)

    def test_rate_limits_timeouts_and_server_errors_are_retried_within_the_limit(self) -> None:
        for error in (HttpError("HTTP 429", 429), HttpError("HTTP 503", 503), HttpError("timeout")):
            with self.subTest(error=str(error)):
                transport = RecordingTransport(error, error, error, error)
                with self.assertRaises(HttpError):
                    source(transport).fetch()
                # Первый вызов плюс ровно max_retries повторов.
                self.assertEqual(len(transport.calls), 3)

    def test_a_retry_can_still_succeed(self) -> None:
        transport = RecordingTransport(HttpError("HTTP 503", 503), response())
        listings = source(transport).fetch()
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(listings), 1)

    def test_a_response_without_listings_fails_only_this_profile(self) -> None:
        transport = RecordingTransport({"success": True, "credits_charged": 1})
        with self.assertRaises(HttpError):
            source(transport).fetch()

    def test_the_api_key_never_reaches_the_logs(self) -> None:
        transport = RecordingTransport(HttpError("HTTP 503", 503), response())
        with self.assertLogs("deal_radar.sources.facebook", level="INFO") as logs:
            source(transport).fetch()
        self.assertTrue(logs.output)
        for line in logs.output:
            self.assertNotIn(FAKE_KEY, line)


class ConfigurationTest(unittest.TestCase):
    def test_the_source_is_disabled_by_default(self) -> None:
        self.assertFalse(FacebookMarketplaceConfig().enabled)

    def test_environment_configures_the_source_and_keeps_the_key_out_of_json(self) -> None:
        raw = {
            "profiles": [{"name": "bazos", "rss_url": "https://sport.bazos.cz/rss.php?hledat=kolo"}],
            "facebook_marketplace": {
                "profiles": [
                    {"name": "Praha", "query": "kolo", "lat": 50.0755, "lng": 14.4378, "radius_km": 60}
                ]
            },
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "FACEBOOK_MARKETPLACE_ENABLED": "true",
                    "SCRAPECREATORS_API_KEY": FAKE_KEY,
                    "FACEBOOK_MARKETPLACE_MAX_PAGES_PER_PROFILE": "2",
                    "FACEBOOK_MARKETPLACE_RESULTS_PER_PAGE": "12",
                },
                clear=False,
            ):
                loaded = load_config(path)
            on_disk = path.read_text(encoding="utf-8")
        self.assertTrue(loaded.facebook_marketplace.enabled)
        self.assertEqual(loaded.facebook_marketplace.api_key, FAKE_KEY)
        self.assertEqual(loaded.facebook_marketplace.max_pages_per_profile, 2)
        self.assertEqual(loaded.facebook_marketplace.results_per_page, 12)
        self.assertNotIn(FAKE_KEY, on_disk)

    def test_credit_budget_is_reported_from_the_active_profiles(self) -> None:
        settings = config(profiles=[profile(), profile(name="Brno", enabled=False)])
        self.assertEqual(len(settings.active_profiles), 1)
        # 20-минутный интервал -> 72 запроса в сутки -> 2160 в месяц на профиль.
        self.assertEqual(settings.credits_per_month(), 2160)
        self.assertEqual(
            FacebookMarketplaceConfig(profiles=[profile(), profile(name="Brno")]).credits_per_month(),
            4320,
        )

    def test_invalid_profiles_are_rejected_at_startup(self) -> None:
        for broken in (
            profile(lat=100.0),
            profile(radius_km=0),
            profile(condition="mint"),
            profile(min_price=5000, max_price=1000),
            profile(query=""),
        ):
            with self.subTest(profile=broken):
                with self.assertRaises(ValueError):
                    FacebookMarketplaceConfig(profiles=[broken]).validate()


class FakeSource:
    def __init__(self, listings: list[Listing]) -> None:
        self.listings = listings
        self.profile = SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")

    def fetch(self) -> list[Listing]:
        return self.listings


class FailingSource:
    label = "facebook:broken"

    def fetch(self) -> list[Listing]:
        raise HttpError("HTTP 402", 402)


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_listing(self, listing: Listing, retail_enabled: bool, analysis=None) -> int:
        self.sent.append(listing.external_id)
        return 100 + len(self.sent)

    def poll_feedback(self, offset: int = 0):
        return [], offset


class ServiceIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def build(self, **overrides: Any) -> AppConfig:
        settings: dict[str, Any] = {
            "database_path": str(Path(self.directory.name) / "state.sqlite3"),
            "bootstrap_mode": "send_all",
            "profiles": [SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")],
            "telegram": TelegramConfig(bot_token="test", chat_id="1"),
            "retail": RetailConfig(enabled=False),
            "market_pricing": MarketPricingConfig(enabled=False),
            "deal_scoring": DealScoringConfig(enabled=True),
            "telegram_gate": TelegramGateConfig(enabled=False),
        }
        settings.update(overrides)
        config = AppConfig(**settings)
        config.validate()
        return config

    def test_a_disabled_source_adds_no_profile_and_costs_nothing(self) -> None:
        service = DealRadarService(self.build(facebook_marketplace=config(enabled=False)))
        try:
            self.assertFalse([item for item in service.sources if "facebook" in str(item.label)])
        finally:
            service.close()

    def test_a_missing_key_disables_only_facebook(self) -> None:
        service = DealRadarService(self.build(facebook_marketplace=config(api_key="")))
        try:
            labels = [str(source.label) for source in service.sources]
            self.assertFalse([label for label in labels if label.startswith("facebook")])
            self.assertTrue(labels)  # Bazoš остался на месте
        finally:
            service.close()

    def test_an_enabled_source_registers_one_adapter_per_active_profile(self) -> None:
        settings = config(profiles=[profile(), profile(name="Brno", enabled=False)])
        service = DealRadarService(self.build(facebook_marketplace=settings))
        try:
            labels = [str(source.label) for source in service.sources]
            self.assertIn("facebook:Praha kola", labels)
            self.assertNotIn("facebook:Brno", labels)
        finally:
            service.close()

    def test_a_facebook_failure_does_not_stop_the_other_sources(self) -> None:
        service = DealRadarService(self.build())
        listing = Listing(
            source="bazos",
            external_id="alive",
            title="Trek Marlin 7 2024",
            description="Kolo po servisu.",
            url="https://bazos.example/alive",
            profile="test",
            price_czk=14900,
            price_amount=14900,
            price_status="numeric",
            published_at=datetime.now(UTC),
        )
        service.sources = [FakeSource([listing]), FailingSource()]
        telegram = FakeTelegram()
        try:
            stats = service.process_once(telegram)
        finally:
            service.close()
        self.assertEqual(stats["new"], 1)
        self.assertEqual(telegram.sent, ["alive"])

    def test_a_facebook_listing_travels_the_whole_pipeline_once(self) -> None:
        transport = RecordingTransport(response(card()))
        settings = config()
        service = DealRadarService(self.build(facebook_marketplace=settings, profiles=[]))
        service.sources = [
            FacebookMarketplaceSource(
                settings.profiles[0], settings, transport=transport, sleeper=lambda _: None
            )
        ]
        telegram = FakeTelegram()
        try:
            first = service.process_once(telegram)
            second = service.process_once(telegram)
            stored = service.storage.get_listing("facebook_marketplace", "1122334455")
            row = service.storage.connection.execute(
                "SELECT sent_at FROM listings WHERE source = 'facebook_marketplace'"
            ).fetchone()
        finally:
            service.close()
        self.assertEqual(first["new"], 1)
        self.assertEqual(second["new"], 0)
        self.assertEqual(telegram.sent, ["1122334455"])  # второй цикл не дублирует
        assert stored is not None
        self.assertEqual(stored.currency, "CZK")
        self.assertIsNotNone(row["sent_at"])

    def test_the_same_id_from_two_profiles_is_sent_once(self) -> None:
        settings = config(profiles=[profile(name="Praha"), profile(name="Praha okolí", radius_km=100)])
        service = DealRadarService(self.build(facebook_marketplace=settings, profiles=[]))
        service.sources = [
            FacebookMarketplaceSource(
                item, settings, transport=RecordingTransport(response(card())), sleeper=lambda _: None
            )
            for item in settings.profiles
        ]
        telegram = FakeTelegram()
        try:
            stats = service.process_once(telegram)
        finally:
            service.close()
        self.assertEqual(stats["fetched"], 1)
        self.assertEqual(telegram.sent, ["1122334455"])

    def test_a_foreign_currency_listing_is_never_priced_in_czk(self) -> None:
        transport = RecordingTransport(
            response(card(price={"formatted_amount": "$300", "amount": 300}))
        )
        settings = config()
        service = DealRadarService(self.build(facebook_marketplace=settings, profiles=[]))
        service.sources = [
            FacebookMarketplaceSource(
                settings.profiles[0], settings, transport=transport, sleeper=lambda _: None
            )
        ]
        try:
            service.process_once(FakeTelegram())
            analysis = service.storage.get_analysis("facebook_marketplace", "1122334455")
        finally:
            service.close()
        assert analysis is not None and analysis.deal_evaluation is not None
        # Без курса кроны не выдумываются: сделка честно уходит в ручную проверку.
        self.assertIsNone(analysis.deal_evaluation.purchase_price_czk)
        self.assertIn("purchase_price_czk", analysis.deal_evaluation.missing_fields)
        self.assertEqual(analysis.deal_evaluation.status, "MANUAL_REVIEW")

    def _facebook_service(self, settings, transport):
        service = DealRadarService(self.build(facebook_marketplace=settings, profiles=[]))
        service.sources = [
            FacebookMarketplaceSource(
                settings.profiles[0], settings, transport=transport, sleeper=lambda _: None
            )
        ]
        return service

    def test_the_cycle_enriches_a_new_facebook_listing(self) -> None:
        settings = config(detail_enabled=True, max_details_per_run=5)
        transport = RecordingTransport(response(), detail_response())
        service = self._facebook_service(settings, transport)
        try:
            service.process_once(FakeTelegram())
            stored = service.storage.get_listing("facebook_marketplace", "1122334455")
        finally:
            service.close()
        assert stored is not None
        self.assertIn("Shimano Deore", stored.description)
        self.assertEqual(len(stored.image_urls), 3)
        self.assertEqual(stored.detail_status, "detail")

    def test_detail_is_not_spent_on_listings_the_hard_filter_rejects(self) -> None:
        settings = config(detail_enabled=True, max_details_per_run=5)
        transport = RecordingTransport(
            response(card(id="helmet", title="Helma POC Axion 55-58")), detail_response()
        )
        service = self._facebook_service(settings, transport)
        try:
            service.process_once(FakeTelegram())
        finally:
            service.close()
        # Только поиск: за шлем платить второй раз незачем.
        self.assertEqual(len(transport.calls), 1)

    def test_the_daily_detail_cap_stops_spending(self) -> None:
        settings = config(detail_enabled=True, max_details_per_run=5, max_details_per_day=1)
        transport = RecordingTransport(
            response(card(id="a"), card(id="b")), detail_response(), detail_response()
        )
        service = self._facebook_service(settings, transport)
        try:
            service.process_once(FakeTelegram())
        finally:
            service.close()
        # Один поиск плюс ровно один detail: дневной потолок держит расход.
        self.assertEqual(len(transport.calls), 2)

    def test_the_daily_cap_survives_a_restart(self) -> None:
        settings = config(detail_enabled=True, max_details_per_run=5, max_details_per_day=1)
        first = self._facebook_service(settings, RecordingTransport(response(card(id="a")), detail_response()))
        try:
            first.process_once(FakeTelegram())
        finally:
            first.close()
        second_transport = RecordingTransport(response(card(id="b")), detail_response())
        second = self._facebook_service(settings, second_transport)
        try:
            second.process_once(FakeTelegram())
        finally:
            second.close()
        self.assertEqual(len(second_transport.calls), 1)  # только поиск

    def test_no_log_line_of_a_cycle_contains_the_key(self) -> None:
        transport = RecordingTransport(response(card()))
        settings = config()
        service = DealRadarService(self.build(facebook_marketplace=settings, profiles=[]))
        service.sources = [
            FacebookMarketplaceSource(
                settings.profiles[0], settings, transport=transport, sleeper=lambda _: None
            )
        ]
        try:
            with self.assertLogs(level=logging.INFO) as logs:
                service.process_once(FakeTelegram())
        finally:
            service.close()
        for line in logs.output:
            self.assertNotIn(FAKE_KEY, line)


if __name__ == "__main__":
    unittest.main()
