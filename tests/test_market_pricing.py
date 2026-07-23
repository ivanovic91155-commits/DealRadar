from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from deal_radar.bike_identity import identify_bike
from deal_radar.config import MarketPricingConfig
from deal_radar.exchange_rates import ExchangeRateProvider
from deal_radar.market_pricing import MarketPriceEngine, match_market_comparable
from deal_radar.models import Listing, MarketComparable, Valuation
from deal_radar.storage import Storage


class StaticSource:
    def __init__(self, name: str, country: str, items: list[MarketComparable] | None = None, fail: bool = False) -> None:
        self.name = name
        self.country = country
        self.items = items or []
        self.fail = fail
        self.request_count = 0

    def search(self, identity):
        self.request_count += 1
        if self.fail:
            raise TimeoutError("source offline")
        return list(self.items)


def comparable(
    external_id: str,
    title: str,
    price: float,
    *,
    country: str = "CZ",
    source: str = "bazos",
    currency: str = "CZK",
    description: str = "Complete bicycle in good condition",
    image_fingerprint: str = "",
) -> MarketComparable:
    return MarketComparable(
        source=source,
        source_listing_id=external_id,
        url=f"https://{source}.example/{external_id}",
        country=country,
        title=title,
        description=description,
        price_original=price,
        currency=currency,
        price_czk=int(price) if currency == "CZK" else None,
        image_fingerprint=image_fingerprint,
    )


def target(external_id: str = "target", title: str = "Trek Marlin 7 Gen 3 2025 29") -> Listing:
    return Listing(
        source="bazos",
        external_id=external_id,
        title=title,
        description="Complete hardtail mountain bike with aluminium frame, Shimano Deore and hydraulic brakes.",
        url=f"https://bazos.example/{external_id}",
        profile="test",
        price_czk=15000,
        price_amount=15000,
        price_status="numeric",
    )


class MarketMatchingTest(unittest.TestCase):
    def test_exact_and_close_year(self) -> None:
        identity = identify_bike("Trek Marlin 7 Gen 3 2025")
        exact = comparable("1", "Trek Marlin 7 Gen 3 2024", 20000)
        close = comparable("2", "Trek Marlin 7 Gen 3 2023", 19000)
        self.assertEqual(match_market_comparable(identity, exact)[0], "exact")
        self.assertEqual(match_market_comparable(identity, close)[0], "close")

    def test_generation_trim_electric_suspension_and_part_conflicts(self) -> None:
        identity = identify_bike(
            "Trek Fuel EX 8 GX Gen 3 2025",
            "Full suspension mountain bike hydraulic brakes aluminium frame",
        )
        different_generation = comparable("g", "Trek Fuel EX 8 GX Gen 2 2025", 30000)
        different_trim = comparable("t", "Trek Fuel EX 8 SX Gen 3 2025", 30000)
        ebike = comparable("e", "Trek Fuel EX 8 GX Gen 3 2025 e-bike", 60000)
        hardtail = comparable(
            "h",
            "Trek Fuel EX 8 GX Gen 3 2025",
            30000,
            description="Hardtail mountain bike hydraulic brakes aluminium frame",
        )
        part = comparable("p", "Trek Fuel EX 8 GX frame only", 10000)
        self.assertEqual(match_market_comparable(identity, different_generation)[2], "generation_mismatch")
        self.assertEqual(match_market_comparable(identity, different_trim)[0], "model_family")
        self.assertEqual(match_market_comparable(identity, ebike)[2], "electric_mismatch")
        self.assertEqual(match_market_comparable(identity, hardtail)[2], "suspension_type_mismatch")
        self.assertEqual(match_market_comparable(identity, part)[0], "rejected")

    def test_model_family_and_component_class(self) -> None:
        family_target = identify_bike("Scott Scale 950 2024 mountain bike")
        family = comparable("f", "Scott Scale 960 2023 mountain bike", 25000)
        self.assertEqual(match_market_comparable(family_target, family)[0], "model_family")

        component_target = identify_bike(
            "Trek Roscoe 7 2024 mountain bike",
            "Hardtail aluminium Shimano Deore hydraulic brakes Rockshox Judy",
        )
        component = comparable(
            "c",
            "Giant Fathom 2 2024 mountain bike",
            26000,
            description="Hardtail aluminium Shimano Deore hydraulic brakes Rockshox Judy",
        )
        self.assertEqual(match_market_comparable(component_target, component)[0], "component_class")


class MarketPriceEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.storage = Storage(str(Path(self.directory.name) / "state.sqlite3"))
        self.storage.save_exchange_rates(
            {"CZK": 1.0, "EUR": 24.0, "PLN": 5.8, "CHF": 26.0},
            datetime.now(UTC).date().isoformat(),
            "test",
            datetime.now(UTC).isoformat(),
        )

    def tearDown(self) -> None:
        self.storage.close()
        self.directory.cleanup()

    def engine(self, sources: list[StaticSource], **overrides) -> MarketPriceEngine:
        config = MarketPricingConfig(
            enabled=True,
            sources=[source.name for source in sources],
            brand_market_mapping={"Trek": ["DE", "EU"], "Scott": ["DE", "EU"]},
        )
        for key, value in overrides.items():
            setattr(config, key, value)
        rates = ExchangeRateProvider(
            self.storage,
            "https://example.invalid/rates",
            fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("cached currency expected")),
        )
        return MarketPriceEngine(config, self.storage, rates, sources)

    def evaluate(self, item: Listing, sources: list[StaticSource], **kwargs):
        self.storage.register([item])
        return self.engine(sources, **kwargs).find(item)

    def test_one_exact_czech_comparable_is_low_confidence_with_range_and_quick_sale(self) -> None:
        source = StaticSource(
            "bazos_cz",
            "CZ",
            [comparable("one", "Trek Marlin 7 Gen 3 2025", 20000)],
        )
        result = self.evaluate(target(), [source])
        self.assertEqual(result.market_price_czk, 20000)
        self.assertEqual(result.quick_sale_price_czk, 17000)
        self.assertEqual((result.price_low_czk, result.price_high_czk), (17000, 23000))
        self.assertEqual(result.confidence, "low")
        self.assertEqual(result.status, "market_price_low_confidence")

    def test_five_czech_comparables_produce_high_confidence_weighted_median(self) -> None:
        prices = (19000, 19500, 20000, 20500, 21000)
        source = StaticSource(
            "bazos_cz",
            "CZ",
            [comparable(str(index), "Trek Marlin 7 Gen 3 2025", price) for index, price in enumerate(prices)],
        )
        result = self.evaluate(target(), [source])
        self.assertEqual(result.market_price_czk, 20000)
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.comparables_unique, 5)
        self.assertEqual(result.countries_used, ["CZ"])

    def test_statistical_outlier_is_removed(self) -> None:
        prices = (19000, 19500, 20000, 20500, 100000)
        source = StaticSource(
            "bazos_cz",
            "CZ",
            [comparable(str(index), "Trek Marlin 7 Gen 3 2025", price) for index, price in enumerate(prices)],
        )
        result = self.evaluate(target(), [source])
        self.assertLess(result.market_price_czk or 0, 30000)
        self.assertTrue(any(item.exclusion_reason == "statistical_price_outlier" for item in result.rejected_comparables))

    def test_foreign_only_is_explicit_and_eur_is_converted(self) -> None:
        de = StaticSource(
            "kleinanzeigen_de",
            "DE",
            [comparable("de", "Trek Marlin 7 Gen 3 2025", 500, country="DE", source="kleinanzeigen_de", currency="EUR")],
        )
        result = self.evaluate(target(), [de])
        self.assertEqual(result.market_price_czk, 12000)
        self.assertEqual(result.status, "foreign_only_estimate")
        self.assertEqual(result.confidence, "low")
        self.assertEqual(result.foreign_comparables, 1)

    def test_czech_and_foreign_disagreement_lowers_confidence(self) -> None:
        cz = StaticSource(
            "bazos_cz",
            "CZ",
            [
                comparable("cz1", "Trek Marlin 7 Gen 3 2025", 19500),
                comparable("cz2", "Trek Marlin 7 Gen 3 2025", 20500),
            ],
        )
        de = StaticSource(
            "kleinanzeigen_de",
            "DE",
            [
                comparable("de1", "Trek Marlin 7 Gen 3 2025", 1600, country="DE", source="kleinanzeigen_de", currency="EUR"),
                comparable("de2", "Trek Marlin 7 Gen 3 2025", 1650, country="DE", source="kleinanzeigen_de", currency="EUR"),
            ],
        )
        result = self.evaluate(target(), [cz, de])
        self.assertEqual(result.confidence, "low")
        self.assertTrue(any("расходятся" in warning for warning in result.warnings))

    def test_duplicate_group_image_and_self_are_counted_once(self) -> None:
        item = target()
        source = StaticSource(
            "bazos_cz",
            "CZ",
            [
                comparable("target", item.title, 15000, source="bazos"),
                comparable("a", item.title, 20000, source="bazos", image_fingerprint="same-photo"),
                comparable("b", item.title, 20000, source="cyklobazar", image_fingerprint="same-photo"),
            ],
        )
        result = self.evaluate(item, [source])
        self.assertEqual(result.comparables_unique, 1)
        self.assertEqual(result.source_copies_excluded, 1)
        self.assertEqual(result.duplicates_removed, 1)

    def test_family_component_depreciation_and_no_data_statuses(self) -> None:
        family_item = target("family", "Scott Scale 950 2024 mountain bike")
        family_source = StaticSource(
            "bazos_cz", "CZ", [comparable("family-c", "Scott Scale 960 2023 mountain bike", 25000)]
        )
        family_result = self.evaluate(family_item, [family_source])
        self.assertEqual(family_result.family_comparables, 1)
        self.assertEqual(family_result.confidence, "low")

        depreciation_item = target("depreciation", "Trek Marlin 7 Gen 3 2023")
        self.storage.register([depreciation_item])
        new_price = Valuation("Trek Marlin 7", "high", median_price_czk=50000, status="success")
        depreciation = self.engine([]).find(depreciation_item, new_valuation=new_price)
        self.assertEqual(depreciation.status, "depreciation_estimate")
        self.assertEqual(depreciation.market_price_czk, 27500)

        no_data_item = target("none", "Trek Roscoe 7 Gen 2 2024")
        self.storage.register([no_data_item])
        no_data = self.engine([]).find(no_data_item)
        self.assertIsNone(no_data.market_price_czk)
        self.assertIn(no_data.status, {"insufficient_comparables", "no_matching_comparables"})

    def test_model_cache_prevents_repeated_http_for_second_listing(self) -> None:
        source = StaticSource(
            "bazos_cz", "CZ", [comparable("cached", "Trek Marlin 7 Gen 3 2025", 20000)]
        )
        engine = self.engine([source])
        first = target("cache-a")
        second = target("cache-b")
        self.storage.register([first, second])
        engine.find(first)
        result = engine.find(second)
        self.assertEqual(source.request_count, 1)
        self.assertGreaterEqual(result.cache_hits, 1)

    def test_source_error_does_not_stop_next_market(self) -> None:
        failed = StaticSource("bazos_cz", "CZ", fail=True)
        german = StaticSource(
            "kleinanzeigen_de",
            "DE",
            [comparable("de", "Trek Marlin 7 Gen 3 2025", 500, country="DE", source="kleinanzeigen_de", currency="EUR")],
        )
        result = self.evaluate(target(), [failed, german])
        self.assertEqual(result.market_price_czk, 12000)
        self.assertEqual(failed.request_count, 1)
        self.assertEqual(german.request_count, 1)

    def test_circuit_breaker_opens_and_skips_third_model(self) -> None:
        failed = StaticSource("bazos_cz", "CZ", fail=True)
        engine = self.engine([failed], circuit_breaker_errors=2)
        for external_id, title in (
            ("m1", "Trek Marlin 7 2025"),
            ("m2", "Trek Roscoe 7 2025"),
            ("m3", "Trek X-Caliber 9 2025"),
        ):
            item = target(external_id, title)
            self.storage.register([item])
            engine.find(item, force=True)
        self.assertEqual(failed.request_count, 2)


if __name__ == "__main__":
    unittest.main()
