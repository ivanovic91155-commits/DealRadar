from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from deal_radar.config import PriorityConfig
from deal_radar.models import Listing, ListingAnalysis, UsedComparable, UsedComparables, Valuation
from deal_radar.storage import Storage


class StorageTest(unittest.TestCase):
    def test_same_contact_requires_matching_price_and_similar_description(self) -> None:
        first = Listing(
            "bazos",
            "seller-a",
            "Trek Marlin 7 29 2025",
            (
                "Complete Trek Marlin bicycle with hydraulic brakes, original drivetrain, "
                "regular service and clean frame. Contact +420 777 123 456."
            ),
            "https://bazos.example/seller-a",
            "test",
            price_czk=15000,
        )
        different_bike = Listing(
            "bazos",
            "seller-b",
            "Trek Marlin 7 29 2025",
            (
                "Second bicycle sold after a crash, damaged fork, worn chain and wheels that "
                "need rebuilding before riding. Contact +420 777 123 456."
            ),
            "https://bazos.example/seller-b",
            "test",
            price_czk=15000,
        )
        with TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "state.sqlite3"))
            storage.register([first, different_bike])
            self.assertEqual(storage.find_used_comparables(first, max_age_days=30).count, 1)

            repost = Listing.from_dict(different_bike.to_dict())
            repost.description = first.description + " Reposted advertisement."
            storage.register([repost])
            self.assertEqual(storage.find_used_comparables(first, max_age_days=30).count, 0)
            storage.close()

    def test_possible_duplicate_is_excluded_from_used_comparables(self) -> None:
        base = (
            "Trek Marlin 7 complete mountain bicycle model 2025 frame L wheels 29 hydraulic brakes "
            "Shimano drivetrain serviced fork clean frame original components ready to ride today"
        )
        words = base.split()
        changed = " ".join(word for index, word in enumerate(words) if index not in set(range(4, 9)))
        changed += " changed condition details"
        first = Listing(
            "bazos", "possible-b", "Trek Marlin 7 29 2025", base,
            "https://bazos.example/possible", "test", price_czk=15000,
        )
        second = Listing(
            "cyklobazar", "possible-c", "Trek Marlin 7 29 2025", changed,
            "https://cyklobazar.example/possible", "test", price_czk=15000,
        )
        with TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "state.sqlite3"))
            storage.register([first, second])
            stats = storage.backfill_duplicates(PriorityConfig())
            self.assertEqual(stats["possible_groups"], 1)
            self.assertEqual(storage.find_used_comparables(first, max_age_days=30).count, 0)
            storage.close()

    def test_duplicate_backfill_is_idempotent_preserves_rows_feedback_and_comparables(self) -> None:
        full_description = (
            "Prodám celoodpružené trailové kolo Merida One-Twenty 400 modelový rok 2023, "
            "velikost rámu L a kola 29. Kolo má zdvih 130 mm vpředu a 120 mm vzadu. "
            "Je vhodné na lesní traily, technický terén, výlety a běžné celodenní ježdění. "
            "Pravidelný servis, teleskopická sedlovka, pohon Shimano Deore a hydraulické brzdy."
        )
        truncated = " ".join(full_description.split()[:30]) + " ..."
        bazos = Listing(
            source="bazos",
            external_id="merida-b",
            title='Merida One-Twenty 400 velikost L kola 29"',
            description=truncated,
            url="https://bazos.example/merida",
            profile="test",
            price_czk=29000,
            price_amount=29000,
            price_status="numeric",
        )
        cyklo = Listing(
            source="cyklobazar",
            external_id="merida-c",
            title="Merida One-Twenty 400",
            description="Trailová kola Praha Merida",
            url="https://cyklobazar.example/merida",
            profile="test",
            price_czk=29000,
            price_amount=29000,
            price_status="numeric",
        )
        with TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "state.sqlite3"))
            storage.register([bazos, cyklo])
            storage.record_feedback("bazos", "merida-b", "interesting")
            storage.save_analysis(
                bazos,
                ListingAnalysis(
                    48,
                    "manual_review",
                    used_comparables=UsedComparables(
                        count=1,
                        minimum_price_czk=29000,
                        maximum_price_czk=29000,
                        median_price_czk=29000,
                        items=[
                            UsedComparable(
                                "cyklobazar",
                                "merida-c",
                                cyklo.title,
                                cyklo.url,
                                29000,
                            )
                        ],
                        confidence="low",
                    ),
                ),
            )
            first = storage.backfill_duplicates(
                PriorityConfig(),
                description_loader=lambda listing: full_description,
            )
            record = storage.get_duplicate_record(bazos)
            detected_at = record["detected_at"] if record else ""
            self.assertEqual(first["confirmed_groups"], 1)
            self.assertIsNotNone(record)
            self.assertTrue(record["is_canonical"])
            self.assertEqual(storage.duplicate_alternatives(bazos)[0]["source"], "cyklobazar")
            self.assertEqual(storage.find_used_comparables(bazos, max_age_days=30).count, 0)
            storage.refresh_duplicate_used_comparables(30)
            refreshed = storage.get_analysis("bazos", "merida-b")
            self.assertEqual(refreshed.used_comparables.count if refreshed and refreshed.used_comparables else None, 0)

            second = storage.backfill_duplicates(
                PriorityConfig(),
                description_loader=lambda listing: full_description,
            )
            repeated = storage.get_duplicate_record(bazos)
            self.assertEqual(first, second)
            self.assertEqual(repeated["detected_at"] if repeated else "", detected_at)
            self.assertEqual(storage.connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 2)
            self.assertEqual(storage.connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 1)
            storage.close()

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
                tables = {
                    row["name"]
                    for row in storage.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertIn("deal_evaluations", tables)
                self.assertIn("deal_cost_overrides", tables)
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
