from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from deal_radar.models import Listing
from deal_radar.storage import Storage


def L(eid, price):
    return Listing(source="bazos", external_id=eid, title="Trek Marlin 7 2024",
                   description="kolo", url=f"https://bazos.cz/inzerat/{eid}/k.php",
                   profile="p", price_czk=price, published_at=datetime.now(UTC))


class PriceHistoryTest(unittest.TestCase):
    def test_records_and_computes_median(self):
        with TemporaryDirectory() as d:
            st = Storage(str(Path(d) / "s.db"))
            key = "trek|marlin-7|?|2024|29"
            for eid, price in [("1", 12000), ("2", 13000), ("3", 14000), ("4", 15000)]:
                st.record_price_observation(L(eid, price), model_key=key,
                                            brand="Trek", model="Marlin 7", model_year=2024)
            stats = st.used_price_stats(key)
            self.assertIsNotNone(stats)
            self.assertEqual(stats["count"], 4)
            self.assertEqual(stats["median_czk"], 13500)
            self.assertEqual(stats["min_czk"], 12000)
            self.assertEqual(stats["max_czk"], 15000)
            st.close()

    def test_idempotent_by_listing(self):
        with TemporaryDirectory() as d:
            st = Storage(str(Path(d) / "s.db"))
            key = "trek|marlin-7|?|2024|29"
            # тот же external_id дважды (TOP-переподнятие) — одна запись
            st.record_price_observation(L("1", 12000), model_key=key)
            st.record_price_observation(L("1", 12000), model_key=key)
            st.record_price_observation(L("2", 13000), model_key=key)
            stats = st.used_price_stats(key, min_samples=1)
            self.assertEqual(stats["count"], 2)
            st.close()

    def test_below_min_samples_returns_none(self):
        with TemporaryDirectory() as d:
            st = Storage(str(Path(d) / "s.db"))
            key = "trek|marlin-7|?|2024|29"
            st.record_price_observation(L("1", 12000), model_key=key)
            self.assertIsNone(st.used_price_stats(key, min_samples=3))
            st.close()

    def test_no_price_or_no_model_skipped(self):
        with TemporaryDirectory() as d:
            st = Storage(str(Path(d) / "s.db"))
            st.record_price_observation(L("1", None), model_key="trek|marlin|?|?|?")
            st.record_price_observation(L("2", 12000), model_key="?|?|?|?|?")
            self.assertIsNone(st.used_price_stats("trek|marlin|?|?|?", min_samples=1))
            st.close()


if __name__ == "__main__":
    unittest.main()
