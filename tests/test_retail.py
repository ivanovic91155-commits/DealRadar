from __future__ import annotations

import unittest

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


class PricingRulesTest(unittest.TestCase):
    def test_rejects_accessory_and_used_products(self) -> None:
        identity = identify_bike("Trek Marlin 7 Gen 3 29 2025")
        accessory = offer("A", 8000, "Trek Marlin 7 Gen 3 frame only 2025")
        used = offer("B", 12000, "Trek Marlin 7 Gen 3 29 2025 used")
        self.assertEqual(evaluate_offer(identity, accessory)[1], "accessory_or_part")
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

    def test_requires_three_exact_shops(self) -> None:
        listing = Listing(
            source="bazos",
            external_id="1",
            title="Trek Marlin 7 Gen 3 29 2025",
            description="",
            url="https://sport.bazos.cz/inzerat/1/bike.php",
            profile="test",
            price_czk=14900,
        )
        config = RetailConfig(min_comparables=3)
        result = NewBikePriceService(config, [FakeSource([offer("A", 22000), offer("B", 23000)])]).find(listing)
        self.assertEqual(result.status, "insufficient_data")
        self.assertIsNone(result.median_price_czk)


if __name__ == "__main__":
    unittest.main()
