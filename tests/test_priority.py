from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from deal_radar.config import PriorityConfig
from deal_radar.models import Listing, ListingAnalysis, UsedComparables, Valuation
from deal_radar.priority import build_analysis, dynamic_lookup_budget, select_lookup_candidates, select_notifications


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def listing(external_id: str = "1", title: str = "Trek Marlin 7 Gen 3 29 2025", price: int = 14900) -> Listing:
    return Listing(
        source="bazos",
        external_id=external_id,
        title=title,
        description="Detailed complete bicycle description with components, condition, service history and frame details.",
        url=f"https://sport.bazos.cz/inzerat/{external_id}/bike.php",
        profile="Praha",
        price_czk=price,
        price_amount=price,
        price_status="numeric",
        location="Praha",
        published_at=NOW - timedelta(hours=1),
    )


def valuation(price: int = 30000, sources: int = 1) -> Valuation:
    return Valuation(
        identified_product="Trek Marlin 7",
        confidence="low" if sources == 1 else "medium",
        status="success",
        median_price_czk=price,
        source_count=sources,
        independent_source_count=sources,
        offer_count=sources,
        new_price_confidence="low" if sources == 1 else "medium",
    )


class DynamicBudgetTest(unittest.TestCase):
    def test_budget_boundaries(self) -> None:
        expected = {0: 0, 1: 1, 10: 10, 11: 10, 30: 10, 31: 15, 70: 15, 71: 15, 100: 20, 500: 20}
        self.assertEqual({count: dynamic_lookup_budget(count) for count in expected}, expected)

    def test_cached_items_do_not_consume_budget_and_best_scores_win(self) -> None:
        items = []
        for index, score in enumerate((20, 90, 70, 60)):
            item = listing(str(index))
            analysis = build_analysis(item, PriorityConfig(), now=NOW)
            analysis.preliminary_priority_score = score
            analysis.cache_used = index == 1
            items.append((item, analysis))
        selected = select_lookup_candidates(items, 2)
        self.assertEqual([item.external_id for item, _ in selected], ["2", "3"])


class PriorityScoreTest(unittest.TestCase):
    def test_score_is_deterministic_bounded_and_reasons_are_saved(self) -> None:
        first = build_analysis(listing(), PriorityConfig(), valuation=valuation(), now=NOW)
        second = build_analysis(listing(), PriorityConfig(), valuation=valuation(), now=NOW)
        self.assertEqual(first.preliminary_priority_score, second.preliminary_priority_score)
        self.assertGreaterEqual(first.preliminary_priority_score, 0)
        self.assertLessEqual(first.preliminary_priority_score, 100)
        self.assertTrue(first.reasons)
        self.assertTrue(first.risks)

    def test_cached_price_and_new_price_difference_raise_score(self) -> None:
        base = build_analysis(listing(), PriorityConfig(), now=NOW)
        live = build_analysis(listing(), PriorityConfig(), valuation=valuation(30000), now=NOW)
        cached = build_analysis(listing(), PriorityConfig(), valuation=valuation(30000), cache_used=True, now=NOW)
        self.assertGreater(live.preliminary_priority_score, base.preliminary_priority_score)
        self.assertEqual(cached.preliminary_priority_score, live.preliminary_priority_score + 10)

    def test_used_comparable_only_affects_score_with_sufficient_confidence(self) -> None:
        weak = UsedComparables(count=2, median_price_czk=30000, confidence="low")
        strong = UsedComparables(count=3, median_price_czk=30000, confidence="medium")
        weak_result = build_analysis(listing(), PriorityConfig(), used_comparables=weak, now=NOW)
        strong_result = build_analysis(listing(), PriorityConfig(), used_comparables=strong, now=NOW)
        self.assertGreater(strong_result.preliminary_priority_score, weak_result.preliminary_priority_score)

    def test_unknown_model_goes_to_manual_review_and_part_is_excluded(self) -> None:
        unknown = build_analysis(listing(title="Unknown custom bicycle"), PriorityConfig(), now=NOW)
        self.assertEqual(unknown.priority_class, "manual_review")
        self.assertTrue(any("Модель не подтверждена" in risk for risk in unknown.risks))
        part = build_analysis(listing(title="Trek Marlin frame only"), PriorityConfig(), now=NOW)
        self.assertEqual(part.priority_class, "excluded")

    def test_suspiciously_low_price_is_manual_review_not_rejected(self) -> None:
        result = build_analysis(listing(price=3000), PriorityConfig(), valuation=valuation(30000), now=NOW)
        self.assertEqual(result.priority_class, "manual_review")
        self.assertTrue(any("подозрительно низкая" in risk for risk in result.risks))


class NotificationSelectionTest(unittest.TestCase):
    def _analysis(self, score: int, classification: str) -> ListingAnalysis:
        return ListingAnalysis(score, classification)

    def test_priority_replaces_fifo_reserves_manual_and_skips_low_or_expired(self) -> None:
        config = PriorityConfig(max_telegram_cards=4, manual_review_reserved_slots=1)
        items = [
            (listing("old"), self._analysis(99, "urgent_candidate"), 7.0),
            (listing("low"), self._analysis(34, "low_priority"), 0.1),
            (listing("interesting"), self._analysis(70, "interesting_candidate"), 0.1),
            (listing("manual"), self._analysis(55, "manual_review"), 0.1),
            (listing("urgent"), self._analysis(90, "urgent_candidate"), 0.1),
        ]
        selected = select_notifications(items, config)
        self.assertEqual([item.external_id for item, _ in selected], ["urgent", "interesting", "manual"])

    def test_ten_card_maximum(self) -> None:
        items = [
            (listing(str(index)), self._analysis(90 - index, "urgent_candidate"), 0.1)
            for index in range(20)
        ]
        self.assertEqual(len(select_notifications(items, PriorityConfig())), 10)


if __name__ == "__main__":
    unittest.main()
