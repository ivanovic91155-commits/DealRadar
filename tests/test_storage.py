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
