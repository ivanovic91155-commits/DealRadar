from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from deal_radar.models import Listing, Valuation
from deal_radar.storage import Storage


class StorageTest(unittest.TestCase):
    def test_deduplicates_and_keeps_pending_until_sent(self) -> None:
        listing = Listing(
            source="bazos",
            external_id="42",
            title="Bike",
            description="",
            url="https://sport.bazos.cz/inzerat/42/bike.php",
            profile="test",
        )
        with TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "state.sqlite3"))
            self.assertEqual(storage.register([listing]), 1)
            self.assertEqual(storage.register([listing]), 0)
            self.assertEqual(len(storage.pending(10)), 1)
            storage.mark_sent(listing, 123)
            self.assertEqual(storage.pending(10), [])
            storage.close()

    def test_valuation_cache_roundtrip(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "state.sqlite3"))
            valuation = Valuation(
                identified_product="Trek Marlin 7",
                confidence="high",
                status="success",
                median_price_czk=23000,
                normalized_model_key="trek|marlin-7|gen-3|2025|29",
            )
            storage.cache_valuation_hours(valuation.normalized_model_key, valuation, 24)
            restored = storage.get_cached_valuation(valuation.normalized_model_key)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.median_price_czk if restored else None, 23000)
            storage.close()


if __name__ == "__main__":
    unittest.main()
