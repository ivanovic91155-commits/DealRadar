from __future__ import annotations

import unittest
from unittest.mock import patch

from deal_radar.bike_identity import identify_bike
from deal_radar.config import RetailConfig
from deal_radar.models import Listing, RetailOffer
from deal_radar.pricing import (
    NewBikePriceService,
    calculate_median,
    convert_to_czk,
    deduplicate_offers,
    discount_percent,
    evaluate_offer,
    remove_price_outliers,
    valuation_cache_key,
)


def offer(seller: str, price: int, title: str = "Trek Marlin 7 Gen 3 29 2025", url: str = "") -> RetailOffer:
    return RetailOffer(
        seller=seller,
        product_name=title,
        price_czk=price,
        url=url or f"https://{seller.casefold().replace(' ', '')}.example/bike",
        availability="in_stock",
        condition="new",
        match_score=0.95,
    )


class FakeSource:
    name = "fixture"

    def __init__(self, offers: list[RetailOffer]) -> None:
        self.offers = offers

    def search(self, identity):
        return list(self.offers)


class BikeIdentityTest(unittest.TestCase):
    def test_normalizes_brand_model_generation_year_and_wheel(self) -> None:
        first = identify_bike('TREK Marlin-7 Gen. 3 29" 2025')
        second = identify_bike('Prodám Trek Marlin 7, generace 3, kola 29 palců, model 2025')
        self.assertEqual(first.normalized_key, "trek|marlin-7|gen-3|2025|29")
        self.assertEqual(first.normalized_key, second.normalized_key)

    def test_distinguishes_generations(self) -> None:
        source = identify_bike("Trek Marlin 7 Gen 3 29 2025")
        candidate = offer("A", 20000, "Trek Marlin 7 Gen 2 29 2025")
        score, reason, _ = evaluate_offer(source, candidate)
        self.assertEqual(score, 0)
        self.assertEqual(reason, "generation_mismatch")

    def test_extracts_kids_and_electric_flags(self) -> None:
        identity = identify_bike("Dětské elektrokolo Woom 4 e-bike")
        self.assertEqual(identity.brand, "Woom")
        self.assertEqual(identity.model, "4")
        self.assertEqual(identity.audience, "kids")
        self.assertTrue(identity.electric)

    def test_decimal_wheel_size_does_not_pollute_model(self) -> None:
        identity = identify_bike('Trek Marlin 7 Gen 3 27,5" 2025')
        self.assertEqual(identity.model, "Marlin 7")
        self.assertEqual(identity.wheel_size, "27.5")

    def test_cache_key_keeps_generations_and_trims_separate(self) -> None:
        first = identify_bike("Trek Marlin 7 Gen 2 Comp 29 2025")
        second = identify_bike("Trek Marlin 7 Gen 3 Comp 29 2025")
        third = identify_bike("Trek Marlin 7 Gen 3 Elite 29 2025")
        self.assertNotEqual(valuation_cache_key(first), valuation_cache_key(second))
        self.assertNotEqual(valuation_cache_key(second), valuation_cache_key(third))


class PricingRulesTest(unittest.TestCase):
    def test_rejects_accessory_and_used_products(self) -> None:
        identity = identify_bike("Trek Marlin 7 Gen 3 29 2025")
        accessory = offer("A", 8000, "Trek Marlin 7 Gen 3 frame only 2025")
        bearing_set = offer("C", 990, "Set Ložisek Trek Marlin 7 Gen 3")
        used = offer("B", 12000, "Trek Marlin 7 Gen 3 29 2025 used")
        self.assertEqual(evaluate_offer(identity, accessory)[1], "accessory_or_part")
        self.assertEqual(evaluate_offer(identity, bearing_set)[1], "accessory_or_part")
        self.assertEqual(evaluate_offer(identity, used)[1], "not_new")

    def test_deduplicates_url_and_seller(self) -> None:
        offers = [
            offer("Shop A", 22000, url="https://a.example/one"),
            offer("Shop A", 21000, url="https://a.example/two"),
            offer("Shop B", 23000, url="https://b.example/one"),
            offer("Shop B", 23000, url="https://b.example/one"),
        ]
        self.assertEqual(len(deduplicate_offers(offers)), 2)

    def test_converts_currency(self) -> None:
        self.assertEqual(convert_to_czk(100, "EUR", {"EUR": 25.2}), 2520)
        with self.assertRaises(ValueError):
            convert_to_czk(100, "USD", {"EUR": 25.2})

    def test_median_for_odd_and_even_counts(self) -> None:
        self.assertEqual(calculate_median([offer("A", 20), offer("B", 30), offer("C", 40)]), 30)
        self.assertEqual(
            calculate_median([offer("A", 20), offer("B", 30), offer("C", 40), offer("D", 50)]),
            35,
        )

    def test_removes_price_outlier(self) -> None:
        values = [offer("A", 22000), offer("B", 23000), offer("C", 24000), offer("D", 95000)]
        self.assertEqual([item.price_czk for item in remove_price_outliers(values)], [22000, 23000, 24000])

    def test_discount_percent(self) -> None:
        self.assertEqual(discount_percent(14900, 22990), 35)
        self.assertIsNone(discount_percent(None, 22990))

    def _listing(self) -> Listing:
        return Listing(
            source="bazos",
            external_id="1",
            title="Trek Marlin 7 Gen 3 29 2025",
            description="",
            url="https://sport.bazos.cz/inzerat/1/bike.php",
            profile="test",
            price_czk=14900,
        )

    def test_one_exact_shop_is_accepted_with_low_confidence(self) -> None:
        result = NewBikePriceService(RetailConfig(), [FakeSource([offer("A", 22000)])]).find(self._listing())
        self.assertEqual(result.status, "success")
        self.assertEqual(result.median_price_czk, 22000)
        self.assertEqual(result.new_price_confidence, "low")
        self.assertEqual(result.offer_count, 1)
        self.assertEqual(result.independent_source_count, 1)
        self.assertEqual(result.comparables[0].url, "https://a.example/bike")

    def test_two_shops_use_average_and_spread_controls_confidence(self) -> None:
        service = NewBikePriceService(RetailConfig(), [FakeSource([offer("A", 22000), offer("B", 26000)])])
        result = service.find(self._listing())
        self.assertEqual(result.median_price_czk, 24000)
        self.assertEqual(result.new_price_confidence, "medium")
        wide = NewBikePriceService(
            RetailConfig(),
            [FakeSource([offer("A", 10000), offer("B", 30000)])],
        ).find(self._listing())
        self.assertEqual(wide.new_price_confidence, "low")

    def test_three_shops_are_high_confidence_and_outlier_is_removed(self) -> None:
        result = NewBikePriceService(
            RetailConfig(),
            [FakeSource([offer("A", 22000), offer("B", 23000), offer("C", 24000), offer("D", 95000)])],
        ).find(self._listing())
        self.assertEqual(result.median_price_czk, 23000)
        self.assertEqual(result.independent_source_count, 3)
        self.assertEqual(result.new_price_confidence, "high")

    def test_duplicate_urls_from_one_shop_are_one_independent_source(self) -> None:
        result = NewBikePriceService(
            RetailConfig(),
            [FakeSource([offer("Shop A", 22000, url="https://a.example/one"), offer("Shop A", 21000, url="https://a.example/two")])],
        ).find(self._listing())
        self.assertEqual(result.offer_count, 2)
        self.assertEqual(result.independent_source_count, 1)

    def test_brand_only_model_conflict_and_year_conflict_are_rejected(self) -> None:
        for bad in (
            offer("A", 22000, "Trek bicycle"),
            offer("A", 22000, "Trek X Caliber 9 29 2025"),
            offer("A", 22000, "Trek Marlin 7 Gen 3 29 2024"),
            offer("A", 22000, "Trek Marlin 7 Gen 3 29 2025 e-bike"),
        ):
            result = NewBikePriceService(RetailConfig(), [FakeSource([bad])]).find(self._listing())
            self.assertIsNone(result.median_price_czk)
            self.assertNotEqual(result.status, "model_discontinued")
            self.assertTrue(result.rejected_offers)
            self.assertTrue(result.rejected_offers[0].exclusion_reason)

    def test_two_sources_no_longer_require_three(self) -> None:
        config = RetailConfig(min_comparables=3)
        result = NewBikePriceService(config, [FakeSource([offer("A", 22000), offer("B", 23000)])]).find(self._listing())
        self.assertEqual(result.status, "success")
        self.assertEqual(result.median_price_czk, 22500)

    def test_retries_429_once_but_does_not_retry_403(self) -> None:
        class FlakySource:
            name = "flaky"

            def __init__(self, message: str) -> None:
                self.message = message
                self.calls = 0

            def search(self, identity):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError(self.message)
                return [offer("A", 22000)]

        retryable = FlakySource("HTTP 429")
        with patch("deal_radar.pricing.time.sleep"):
            result = NewBikePriceService(RetailConfig(), [retryable]).find(self._listing())
        self.assertEqual(retryable.calls, 2)
        self.assertEqual(result.status, "success")

        forbidden = FlakySource("HTTP 403")
        result = NewBikePriceService(RetailConfig(), [forbidden]).find(self._listing())
        self.assertEqual(forbidden.calls, 1)
        self.assertEqual(result.status, "source_error")


if __name__ == "__main__":
    unittest.main()
