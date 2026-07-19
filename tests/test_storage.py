from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from deal_radar.models import Listing, Valuation
from deal_radar.storage import Storage


class StorageTest(unittest.TestCase):
    def test_feedback_migration_preserves_old_rows_and_is_repeatable(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    telegram_user TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO feedback
                (source, external_id, label, telegram_user, created_at)
                VALUES ('bazos', '42', 'interesting', '', '2026-07-19T00:00:00+00:00');
                """
            )
            connection.commit()
            connection.close()

            for _ in range(2):
                storage = Storage(str(path))
                columns = {
                    row["name"] for row in storage.connection.execute("PRAGMA table_info(feedback)")
                }
                self.assertIn("callback_query_id", columns)
                self.assertIn("telegram_message_id", columns)
                self.assertIn("telegram_chat_id", columns)
                listing_columns = {
                    row["name"] for row in storage.connection.execute("PRAGMA table_info(listings)")
                }
                self.assertIn("analysis_json", listing_columns)
                self.assertIn("notification_status", listing_columns)
                self.assertEqual(
                    storage.connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0],
                    1,
                )
                storage.close()

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

    def test_used_comparables_exclude_current_duplicate_missing_part_old_and_other_model(self) -> None:
        def bike(external_id: str, price: int | None, title: str = "Trek Marlin 7 Gen 3 29 2025", url: str = "") -> Listing:
            return Listing(
                source="bazos",
                external_id=external_id,
                title=title,
                description="complete bike",
                url=url or f"https://example.test/{external_id}",
                profile="test",
                price_czk=price,
                price_amount=price,
                price_status="numeric" if price else "missing",
            )

        with TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "state.sqlite3"))
            current = bike("current", 10000)
            valid = bike("valid", 14000)
            duplicate = bike("duplicate", 15000, url=valid.url)
            missing = bike("missing", None)
            part = bike("part", 5000, title="Trek Marlin 7 frame only")
            other = bike("other", 16000, title="Trek X Caliber 9 29 2025")
            old = bike("old", 13000)
            storage.register([current, valid, duplicate, missing, part, other, old])
            storage.connection.execute(
                "UPDATE listings SET first_seen_at = ?, last_seen_at = ? WHERE external_id = 'old'",
                (
                    (datetime.now(UTC) - timedelta(days=31)).isoformat(),
                    (datetime.now(UTC) - timedelta(days=31)).isoformat(),
                ),
            )
            storage.connection.commit()
            result = storage.find_used_comparables(current, max_age_days=30)
            self.assertEqual(result.count, 1)
            self.assertEqual(result.confidence, "low")
            self.assertEqual(result.items[0].external_id, "valid")
            storage.close()

    def test_three_used_comparables_have_separate_median(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "state.sqlite3"))
            items = [
                Listing(
                    source="bazos",
                    external_id=str(index),
                    title="Trek Marlin 7 Gen 3 29 2025",
                    description="complete bike",
                    url=f"https://example.test/{index}",
                    profile="test",
                    price_czk=price,
                    price_amount=price,
                    price_status="numeric",
                )
                for index, price in enumerate((10000, 12000, 14000, 16000))
            ]
            storage.register(items)
            result = storage.find_used_comparables(items[0], max_age_days=30)
            self.assertEqual(result.count, 3)
            self.assertEqual(result.median_price_czk, 14000)
            self.assertEqual(result.confidence, "medium")
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

    def test_feedback_callback_id_is_idempotent_and_keeps_message_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "state.sqlite3"))
            first = storage.record_feedback(
                "cyklobazar",
                "abc123",
                "too_expensive",
                callback_query_id="query-1",
                telegram_message_id=77,
                telegram_chat_id="chat",
            )
            second = storage.record_feedback(
                "cyklobazar",
                "abc123",
                "too_expensive",
                callback_query_id="query-1",
                telegram_message_id=77,
                telegram_chat_id="chat",
            )
            row = storage.connection.execute(
                "SELECT label, telegram_message_id, telegram_chat_id, COUNT(*) AS count FROM feedback"
            ).fetchone()
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(row["count"], 1)
            self.assertEqual(row["label"], "too_expensive")
            self.assertEqual(row["telegram_message_id"], 77)
            self.assertEqual(row["telegram_chat_id"], "chat")
            storage.close()


if __name__ == "__main__":
    unittest.main()
