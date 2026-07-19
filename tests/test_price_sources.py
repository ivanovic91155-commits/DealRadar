from __future__ import annotations

from pathlib import Path
import unittest
from urllib.parse import urlparse

from deal_radar.config import RetailConfig
from deal_radar.models import Listing
from deal_radar.price_sources import ZboziPriceSource, extract_next_data
from deal_radar.pricing import NewBikePriceService


FIXTURES = Path(__file__).parent / "fixtures"


class FixtureFetcher:
    def __call__(self, url: str, timeout: int) -> bytes:
        path = urlparse(url).path
        if path.startswith("/hledej"):
            return (FIXTURES / "zbozi_search.html").read_bytes()
        if path.startswith("/vyrobek"):
            return (FIXTURES / "zbozi_product.html").read_bytes()
        raise AssertionError(f"Unexpected fixture URL: {url}")


class ZboziIntegrationTest(unittest.TestCase):
    def test_extracts_embedded_next_data(self) -> None:
        data = extract_next_data((FIXTURES / "zbozi_search.html").read_bytes())
        self.assertIn("props", data)

    def test_search_fixture_produces_median_from_three_shops(self) -> None:
        source = ZboziPriceSource(
            max_queries=1,
            max_product_pages=1,
            max_parallel_requests=1,
            fetcher=FixtureFetcher(),
        )
        config = RetailConfig(min_comparables=3, max_queries_per_source=1, max_product_pages=1)
        listing = Listing(
            source="bazos",
            external_id="42",
            title='Trek Marlin 7 Gen 3 29" 2025: 14 900',
            description="Kompletní horské kolo.",
            url="https://sport.bazos.cz/inzerat/42/trek.php",
            profile="test",
            price_czk=14900,
        )
        valuation = NewBikePriceService(config, [source]).find(listing)
        self.assertEqual(valuation.status, "success")
        self.assertEqual(valuation.median_price_czk, 23000)
        self.assertEqual(valuation.source_count, 3)
        self.assertEqual(valuation.discount_percent, 35)
        self.assertEqual({item.seller for item in valuation.comparables}, {"Shop A", "Shop B", "Shop C"})


if __name__ == "__main__":
    unittest.main()
