from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from deal_radar.ai.budget import BudgetGuard, start_of_day
from deal_radar.config import AIConfig
from deal_radar.models import AIAnalysis, Listing
from deal_radar.storage import Storage


def listing(external_id: str = "1") -> Listing:
    return Listing(
        source="bazos",
        external_id=external_id,
        profile="test",
        title="Trek Marlin 7",
        url=f"https://example.test/{external_id}",
        description="",
    )


def call_record(cost: float, started_at: datetime) -> dict[str, object]:
    return {
        "request_id": f"req-{started_at.timestamp()}-{cost}",
        "listing_source": "bazos",
        "listing_external_id": "1",
        "model_name": "gpt-5.6-luna",
        "started_at": started_at.isoformat(),
        "finished_at": started_at.isoformat(),
        "estimated_total_cost_usd": cost,
        "success": 1,
    }


class BudgetGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.storage = Storage(str(Path(self.directory.name) / "deal_radar.sqlite3"))
        self.now = datetime(2026, 8, 8, 15, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.storage.connection.close()
        self.directory.cleanup()

    def guard(self, **overrides: object) -> BudgetGuard:
        settings: dict[str, object] = {"daily_budget_usd": 5.0}
        settings.update(overrides)
        return BudgetGuard(AIConfig(**settings), self.storage)  # type: ignore[arg-type]

    def test_empty_log_reports_a_clean_budget(self) -> None:
        state = self.guard().state(self.now)
        self.assertEqual(state.spent_usd, 0.0)
        self.assertEqual(state.calls, 0)
        self.assertFalse(state.stopped)
        self.assertFalse(state.warned)
        self.assertEqual(state.remaining_usd, 5.0)

    def test_spend_accumulates_across_calls(self) -> None:
        for cost in (0.5, 0.25, 0.25):
            self.storage.log_ai_call(call_record(cost, self.now))
        state = self.guard().state(self.now)
        self.assertAlmostEqual(state.spent_usd, 1.0)
        self.assertEqual(state.calls, 3)
        self.assertEqual(state.percent_used, 20.0)

    def test_warning_threshold_trips_before_the_hard_stop(self) -> None:
        self.storage.log_ai_call(call_record(4.2, self.now))
        state = self.guard().state(self.now)
        self.assertTrue(state.warned)
        self.assertFalse(state.stopped)

    def test_reaching_the_limit_stops_further_calls(self) -> None:
        self.storage.log_ai_call(call_record(5.0, self.now))
        state = self.guard().state(self.now)
        self.assertTrue(state.stopped)
        self.assertEqual(state.remaining_usd, 0.0)

    def test_stop_at_budget_false_only_warns(self) -> None:
        self.storage.log_ai_call(call_record(9.0, self.now))
        state = self.guard(stop_at_budget=False).state(self.now)
        self.assertTrue(state.warned)
        self.assertFalse(state.stopped)

    def test_yesterdays_spend_does_not_count_against_today(self) -> None:
        self.storage.log_ai_call(call_record(9.0, self.now - timedelta(days=1)))
        state = self.guard().state(self.now)
        self.assertEqual(state.spent_usd, 0.0)
        self.assertFalse(state.stopped)

    def test_budget_survives_a_restart_because_it_reads_the_log(self) -> None:
        self.storage.log_ai_call(call_record(5.0, self.now))
        path = self.storage.connection.execute("PRAGMA database_list").fetchone()["file"]
        self.storage.connection.close()
        self.storage = Storage(path)
        self.assertTrue(self.guard().state(self.now).stopped)

    def test_zero_limit_never_stops_and_reports_no_percentage(self) -> None:
        self.storage.log_ai_call(call_record(3.0, self.now))
        state = self.guard(daily_budget_usd=0.0).state(self.now)
        self.assertFalse(state.stopped)
        self.assertFalse(state.warned)
        self.assertEqual(state.percent_used, 0.0)

    def test_day_boundary_is_utc_midnight(self) -> None:
        self.assertEqual(
            start_of_day(datetime(2026, 8, 8, 23, 59, tzinfo=UTC)),
            datetime(2026, 8, 8, 0, 0, tzinfo=UTC),
        )


class AIAnalysisCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.storage = Storage(str(Path(self.directory.name) / "deal_radar.sqlite3"))

    def tearDown(self) -> None:
        self.storage.connection.close()
        self.directory.cleanup()

    def analysis(self, **overrides: str) -> AIAnalysis:
        defaults = {
            "status": "AI_OK",
            "content_hash": "hash-a",
            "prompt_name": "listing-analysis",
            "prompt_version": "v1.0.0",
            "schema_version": "dealradar.ai-analysis.v1",
            "model_name": "gpt-5.6-luna",
        }
        defaults.update(overrides)
        return AIAnalysis(**defaults)  # type: ignore[arg-type]

    def key(self, analysis: AIAnalysis) -> dict[str, str]:
        return {
            "content_hash": analysis.content_hash,
            "prompt_name": analysis.prompt_name,
            "prompt_version": analysis.prompt_version,
            "schema_version": analysis.schema_version,
            "model_name": analysis.model_name,
        }

    def test_round_trip_returns_the_stored_analysis(self) -> None:
        stored = self.analysis()
        self.storage.cache_ai_analysis(listing(), stored, hours=24)
        loaded = self.storage.get_cached_ai_analysis(**self.key(stored))
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.status, "AI_OK")
        self.assertEqual(loaded.content_hash, "hash-a")

    def test_miss_returns_none(self) -> None:
        self.assertIsNone(
            self.storage.get_cached_ai_analysis(**self.key(self.analysis(content_hash="other")))
        )

    def test_every_key_part_separates_cache_entries(self) -> None:
        self.storage.cache_ai_analysis(listing(), self.analysis(), hours=24)
        for part in ("content_hash", "prompt_version", "schema_version", "model_name"):
            with self.subTest(part=part):
                key = self.key(self.analysis())
                key[part] = "changed"
                self.assertIsNone(self.storage.get_cached_ai_analysis(**key))

    def test_expired_entry_is_dropped_on_read(self) -> None:
        stored = self.analysis()
        self.storage.cache_ai_analysis(listing(), stored, hours=1)
        self.storage.connection.execute(
            "UPDATE ai_analysis_cache SET expires_at = ?",
            ((datetime.now(UTC) - timedelta(hours=1)).isoformat(),),
        )
        self.storage.connection.commit()
        self.assertIsNone(self.storage.get_cached_ai_analysis(**self.key(stored)))
        remaining = self.storage.connection.execute(
            "SELECT COUNT(*) AS total FROM ai_analysis_cache"
        ).fetchone()
        self.assertEqual(remaining["total"], 0)

    def test_rewriting_the_same_key_updates_in_place(self) -> None:
        self.storage.cache_ai_analysis(listing(), self.analysis(), hours=24)
        self.storage.cache_ai_analysis(listing(), self.analysis(status="AI_FAILED"), hours=24)
        rows = self.storage.connection.execute(
            "SELECT COUNT(*) AS total FROM ai_analysis_cache"
        ).fetchone()
        self.assertEqual(rows["total"], 1)
        loaded = self.storage.get_cached_ai_analysis(**self.key(self.analysis()))
        assert loaded is not None
        self.assertEqual(loaded.status, "AI_FAILED")

    def test_call_log_never_stores_request_bodies_or_headers(self) -> None:
        self.storage.log_ai_call(call_record(0.001, datetime.now(UTC)))
        columns = {
            str(row["name"])
            for row in self.storage.connection.execute("PRAGMA table_info(ai_call_log)")
        }
        self.assertFalse(columns & {"api_key", "authorization", "request_body", "prompt", "payload"})


if __name__ == "__main__":
    unittest.main()
