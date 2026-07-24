from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from deal_radar.config import (
    AppConfig,
    DealScoringConfig,
    MarketPricingConfig,
    RetailConfig,
    SearchProfile,
    TelegramConfig,
)
from deal_radar.bike_identity import identify_bike
from deal_radar.models import Listing, MarketValuation, Valuation
from deal_radar.pricing import valuation_cache_key
from deal_radar.service import DealRadarService


class FakeSource:
    def __init__(self, listings: list[Listing]) -> None:
        self.listings = listings
        self.profile = SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")

    def fetch(self) -> list[Listing]:
        return self.listings


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_listing(self, listing: Listing, retail_enabled: bool, analysis=None) -> int:
        self.sent.append(listing.external_id)
        return 100 + len(self.sent)

    def poll_feedback(self, offset: int = 0):
        return [], offset


def make_listing(external_id: str, minutes: int) -> Listing:
    return Listing(
        source="bazos",
        external_id=external_id,
        title="Trek Marlin 7 Gen 3 29 2025",
        description="Complete bike",
        url=f"https://sport.bazos.cz/inzerat/{external_id}/bike.php",
        profile="test",
        published_at=datetime.now(UTC) + timedelta(minutes=minutes),
        price_czk=14900,
        price_amount=14900,
        price_status="numeric",
    )


class ServiceBootstrapTest(unittest.TestCase):
    def test_stage_2_2_reuses_single_stage_2_1_result_and_persists_deal(self) -> None:
        class MarketFinder:
            def __init__(self) -> None:
                self.calls = 0

            def find(self, item, identity, new_valuation=None):
                self.calls += 1
                return MarketValuation(
                    listing_source=item.source,
                    listing_external_id=item.external_id,
                    market_price_czk=20000,
                    quick_sale_price_czk=17000,
                    price_low_czk=18000,
                    price_high_czk=22000,
                    confidence="medium",
                    valuation_method="weighted_median",
                    status="market_price_found",
                    comparables_total=3,
                    comparables_unique=3,
                    exact_comparables=3,
                    cz_comparables=3,
                    countries_used=["CZ"],
                    sources_used=["bazos_cz"],
                )

        with TemporaryDirectory() as directory:
            config = AppConfig(
                database_path=str(Path(directory) / "state.sqlite3"),
                bootstrap_mode="send_all",
                profiles=[
                    SearchProfile(
                        name="test",
                        rss_url="https://sport.bazos.cz/rss.php?hledat=kolo",
                    )
                ],
                telegram=TelegramConfig(bot_token="test", chat_id="1"),
                retail=RetailConfig(enabled=False),
                market_pricing=MarketPricingConfig(enabled=True),
                deal_scoring=DealScoringConfig(enabled=True),
            )
            item = make_listing("stage22", 1)
            item.description = "Kolo je po servisu, ve velmi dobrem stavu a bez investic."
            item.price_czk = 10000
            item.price_amount = 10000
            finder = MarketFinder()
            service = DealRadarService(config)
            service.sources = [FakeSource([item])]
            service._market_finder = lambda: finder  # type: ignore[method-assign]
            telegram = FakeTelegram()
            try:
                first = service.process_once(telegram)
                second = service.process_once(telegram)
                saved = service.storage.get_latest_deal_evaluation("bazos", "stage22")
                saved_analysis = service.storage.get_analysis("bazos", "stage22")
                self.assertEqual(finder.calls, 1)
                self.assertEqual(first["deal_hot"], 1)
                self.assertEqual(first["sent"], 1)
                self.assertEqual(second["new"], 0)
                self.assertEqual(telegram.sent, ["stage22"])
                self.assertEqual(saved.status if saved else None, "HOT")
                self.assertEqual(
                    saved_analysis.deal_evaluation.status
                    if saved_analysis and saved_analysis.deal_evaluation
                    else None,
                    "HOT",
                )
                self.assertEqual(
                    service.storage.connection.execute(
                        "SELECT COUNT(*) FROM deal_evaluations"
                    ).fetchone()[0],
                    1,
                )
            finally:
                service.close()

    def test_confirmed_cross_source_duplicate_sends_only_canonical_and_keeps_both(self) -> None:
        class Finder:
            def identify(self, listing):
                return identify_bike(listing.title)

            def find(self, listing, identity):
                return Valuation(
                    identified_product=identity.display_name,
                    confidence="low",
                    status="success",
                    normalized_model_key=valuation_cache_key(identity),
                    median_price_czk=50000,
                    source_count=1,
                    independent_source_count=1,
                    offer_count=1,
                    new_price_confidence="low",
                )

            def cache_ttl_hours(self, status):
                return 24

        description = (
            "Complete Merida One-Twenty 400 trail bicycle, model year 2023, frame size L, "
            "29 inch wheels, Shimano Deore drivetrain, hydraulic brakes and regular service history."
        )
        first = Listing(
            source="bazos",
            external_id="duplicate-b",
            title="Merida One-Twenty 400 29 2023",
            description=description,
            url="https://bazos.example/merida",
            profile="test",
            price_czk=29000,
            price_amount=29000,
            price_status="numeric",
            published_at=datetime.now(UTC),
        )
        second = Listing(
            source="cyklobazar",
            external_id="duplicate-c",
            title="Merida One-Twenty 400 29 2023",
            description=description,
            url="https://cyklobazar.example/merida",
            profile="test",
            price_czk=29000,
            price_amount=29000,
            price_status="numeric",
            published_at=datetime.now(UTC),
        )
        with TemporaryDirectory() as directory:
            config = AppConfig(
                database_path=str(Path(directory) / "state.sqlite3"),
                bootstrap_mode="send_all",
                profiles=[SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")],
                telegram=TelegramConfig(bot_token="test", chat_id="1"),
                retail=RetailConfig(enabled=True, lookup_delay_seconds=0),
            )
            service = DealRadarService(config)
            telegram = FakeTelegram()
            service.sources = [FakeSource([first, second])]
            service._retail_finder = lambda: Finder()  # type: ignore[method-assign]
            try:
                stats = service.process_once(telegram)
                self.assertEqual(telegram.sent, ["duplicate-b"])
                self.assertEqual(stats["duplicate_groups"], 1)
                self.assertEqual(stats["confirmed_duplicate_suppressed"], 1)
                self.assertEqual(
                    service.storage.connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0],
                    2,
                )
                canonical_analysis = service.storage.get_analysis("bazos", "duplicate-b")
                duplicate_analysis = service.storage.get_analysis("cyklobazar", "duplicate-c")
                self.assertEqual(canonical_analysis.duplicate_alternatives[0]["source"], "cyklobazar")
                self.assertEqual(duplicate_analysis.notification_reason, "confirmed_cross_source_duplicate")
            finally:
                service.close()

    def test_first_run_sends_only_latest_then_only_new_ids(self) -> None:
        with TemporaryDirectory() as directory:
            config = AppConfig(
                database_path=str(Path(directory) / "state.sqlite3"),
                bootstrap_mode="send_latest",
                max_initial_notifications=1,
                profiles=[SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")],
                telegram=TelegramConfig(bot_token="test", chat_id="1"),
                retail=RetailConfig(enabled=False),
            )
            service = DealRadarService(config)
            source = FakeSource([make_listing("1", 1), make_listing("2", 2), make_listing("3", 3)])
            telegram = FakeTelegram()
            service.sources = [source]
            service._telegram = lambda: telegram  # type: ignore[method-assign]
            try:
                first = service.process_once()
                self.assertEqual(first["sent"], 1)
                self.assertEqual(telegram.sent, ["3"])

                second = service.process_once()
                self.assertEqual(second["sent"], 0)

                source.listings.append(make_listing("4", 4))
                third = service.process_once()
                self.assertEqual(third["sent"], 1)
                self.assertEqual(telegram.sent, ["3", "4"])
            finally:
                service.close()

    def test_price_lookup_reorders_notifications_and_all_listings_stay_saved(self) -> None:
        class Finder:
            def identify(self, listing):
                return identify_bike(listing.title)

            def find(self, listing, identity):
                price = 60000 if listing.external_id == "1" else 20000
                return Valuation(
                    identified_product=identity.display_name,
                    confidence="low",
                    status="success",
                    normalized_model_key=valuation_cache_key(identity),
                    median_price_czk=price,
                    source_count=1,
                    independent_source_count=1,
                    offer_count=1,
                    new_price_confidence="low",
                )

            def cache_ttl_hours(self, status):
                return 24

        with TemporaryDirectory() as directory:
            config = AppConfig(
                database_path=str(Path(directory) / "state.sqlite3"),
                bootstrap_mode="send_all",
                profiles=[SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")],
                telegram=TelegramConfig(bot_token="test", chat_id="1"),
                retail=RetailConfig(enabled=True, lookup_delay_seconds=0),
            )
            service = DealRadarService(config)
            telegram = FakeTelegram()
            service.sources = [FakeSource([make_listing("2", 2), make_listing("1", 1)])]
            service._retail_finder = lambda: Finder()  # type: ignore[method-assign]
            try:
                result = service.process_once(telegram)
                self.assertEqual(telegram.sent, ["1", "2"])
                self.assertEqual(result["new"], 2)
                self.assertEqual(
                    service.storage.connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0],
                    2,
                )
            finally:
                service.close()

    def test_cached_price_does_not_call_finder_or_spend_budget(self) -> None:
        class CachedFinder:
            def identify(self, listing):
                return identify_bike(listing.title)

            def as_cached(self, value):
                value.status = "cached"
                value.cache_used = True
                return value

            def find(self, listing, identity):
                raise AssertionError("cache should prevent lookup")

        with TemporaryDirectory() as directory:
            config = AppConfig(
                database_path=str(Path(directory) / "state.sqlite3"),
                bootstrap_mode="send_all",
                profiles=[SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")],
                telegram=TelegramConfig(bot_token="test", chat_id="1"),
                retail=RetailConfig(enabled=True, lookup_delay_seconds=0),
            )
            service = DealRadarService(config)
            item = make_listing("cached", 1)
            identity = identify_bike(item.title)
            service.storage.cache_valuation_hours(
                valuation_cache_key(identity),
                Valuation(
                    identified_product=identity.display_name,
                    confidence="low",
                    status="success",
                    normalized_model_key=valuation_cache_key(identity),
                    median_price_czk=30000,
                    source_count=1,
                    independent_source_count=1,
                    offer_count=1,
                    new_price_confidence="low",
                ),
                24,
            )
            service.sources = [FakeSource([item])]
            service._retail_finder = lambda: CachedFinder()  # type: ignore[method-assign]
            try:
                result = service.process_once(FakeTelegram())
                self.assertEqual(result["cache_hits"], 1)
                self.assertEqual(result["price_lookups"], 0)
            finally:
                service.close()

    def test_price_failure_does_not_block_listing_alert(self) -> None:
        class FailingFinder:
            def identify(self, listing):
                return identify_bike("Trek Marlin 7 Gen 3 29 2025")

            def find(self, listing, identity):
                raise RuntimeError("price source failed")

        with TemporaryDirectory() as directory:
            config = AppConfig(
                database_path=str(Path(directory) / "state.sqlite3"),
                profiles=[SearchProfile(name="test", rss_url="https://www.bazos.cz/rss.php?rub=sp")],
                telegram=TelegramConfig(bot_token="test", chat_id="1"),
                retail=RetailConfig(enabled=True),
            )
            service = DealRadarService(config)
            telegram = FakeTelegram()
            service.sources = [FakeSource([make_listing("9", 1)])]
            service._telegram = lambda: telegram  # type: ignore[method-assign]
            service._retail_finder = lambda: FailingFinder()  # type: ignore[method-assign]
            try:
                result = service.process_once()
                self.assertEqual(result["sent"], 1)
                self.assertEqual(result["price_lookups"], 1)
                self.assertEqual(telegram.sent, ["9"])
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
