from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import unittest
from unittest.mock import patch

from deal_radar.config import DealScoringConfig, load_config
from deal_radar.deal_scoring import (
    DealEvaluator,
    calculate_liquidity,
    detect_condition,
    select_deal_notifications,
)
from deal_radar.models import (
    BikeIdentity,
    DealCosts,
    Listing,
    ListingAnalysis,
    MarketValuation,
)
from deal_radar.storage import Storage


def listing(
    external_id: str = "deal",
    *,
    price: int = 10000,
    description: str = "Trek Marlin je po servisu, ve velmi dobrem stavu a bez investic.",
    title: str = "Trek Marlin 7 Gen 3 2024",
) -> Listing:
    return Listing(
        source="bazos",
        external_id=external_id,
        title=title,
        description=description,
        url=f"https://bazos.example/{external_id}",
        profile="test",
        price_czk=price,
        price_amount=price,
        price_status="numeric",
    )


def market(
    external_id: str = "deal",
    *,
    median: int = 19000,
    quick: int | None = 16000,
    confidence: str = "medium",
    exact: int = 3,
    close: int = 0,
    family: int = 0,
    component: int = 0,
    countries: list[str] | None = None,
    sources: list[str] | None = None,
    status: str = "market_price_found",
) -> MarketValuation:
    count = exact + close + family + component
    countries = countries or ["CZ"]
    sources = sources or ["bazos_cz"]
    return MarketValuation(
        listing_source="bazos",
        listing_external_id=external_id,
        market_price_czk=median,
        quick_sale_price_czk=quick,
        price_low_czk=median - 2000,
        price_high_czk=median + 2000,
        confidence=confidence,
        valuation_method="weighted_median",
        status=status,
        comparables_total=count,
        comparables_unique=count,
        exact_comparables=exact,
        close_comparables=close,
        family_comparables=family,
        component_comparables=component,
        cz_comparables=count if "CZ" in countries else 0,
        foreign_comparables=count if "CZ" not in countries else 0,
        countries_used=countries,
        sources_used=sources,
    )


def analysis(item: Listing, valuation: MarketValuation | None) -> ListingAnalysis:
    return ListingAnalysis(
        preliminary_priority_score=70,
        priority_class="interesting_candidate",
        identity=BikeIdentity(
            brand="Trek",
            model="Marlin 7",
            model_year=2024,
            bike_type="mountain",
            confidence=0.9,
        ),
        market_valuation=valuation,
    )


class DealFormulaAndStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = DealScoringConfig(enabled=True)
        self.evaluator = DealEvaluator(self.config)

    def evaluate(self, item: Listing, valuation: MarketValuation | None, costs: DealCosts | None = None):
        return self.evaluator.evaluate(item, analysis(item, valuation), costs)

    def test_formula_and_rounding_are_exact(self) -> None:
        item = listing(price=10001)
        result = self.evaluate(
            item,
            market(quick=17000),
            DealCosts(100, 50, 25),
        )
        self.assertEqual(result.base_investment_czk, 10176.0)
        self.assertEqual(result.risk_reserve_czk, 1017.6)
        self.assertEqual(result.total_investment_czk, 11193.6)
        self.assertEqual(result.net_profit_czk, 5806.4)
        self.assertEqual(result.roi_percent, 51.87)

    def test_hot_by_profit_and_roi(self) -> None:
        result = self.evaluate(listing(), market(quick=16000))
        self.assertEqual(result.status, "HOT")
        self.assertEqual(result.net_profit_czk, 5000)
        self.assertEqual(result.roi_percent, 45.45)

    def test_boundary_roi_15_to_20_is_interesting(self) -> None:
        result = self.evaluate(listing(price=20000), market(median=30500, quick=26000))
        self.assertEqual(result.net_profit_czk, 4000)
        self.assertEqual(result.roi_percent, 18.18)
        self.assertEqual(result.status, "INTERESTING")

    def test_hot_by_very_high_roi_with_profit_under_4000(self) -> None:
        result = self.evaluate(listing(price=1500), market(median=4100, quick=3500))
        self.assertEqual(result.net_profit_czk, 1850)
        self.assertGreaterEqual(result.roi_percent or 0, 100)
        self.assertEqual(result.status, "HOT")

    def test_negative_profit_and_zero_roi_are_rejected(self) -> None:
        negative = self.evaluate(listing("negative"), market("negative", quick=9000))
        zero = self.evaluate(listing("zero"), market("zero", quick=11000))
        self.assertEqual(negative.status, "REJECT")
        self.assertEqual(zero.status, "REJECT")

    def test_unconfirmed_model_blocks_hot_for_manual_review(self) -> None:
        item = listing("unconfirmed")
        item_analysis = analysis(item, market("unconfirmed", quick=16000))
        item_analysis.identity.model_source = "tail"
        result = self.evaluator.evaluate(item, item_analysis, None)
        self.assertEqual(result.status, "MANUAL_REVIEW")
        self.assertIn("unconfirmed_model", result.flags)

    def test_legacy_identity_without_model_source_still_reaches_hot(self) -> None:
        item = listing("legacy")
        item_analysis = analysis(item, market("legacy", quick=16000))
        self.assertEqual(item_analysis.identity.model_source, "")
        result = self.evaluator.evaluate(item, item_analysis, None)
        self.assertEqual(result.status, "HOT")

    def test_low_confidence_blocks_hot_for_manual_review(self) -> None:
        result = self.evaluate(
            listing("low-confidence"),
            market("low-confidence", quick=18000, confidence="low"),
        )
        self.assertEqual(result.status, "MANUAL_REVIEW")
        self.assertIn("low_market_confidence", result.flags)
        self.assertIsNotNone(result.net_profit_czk)

    def test_service_required_blocks_hot_for_manual_review(self) -> None:
        item = listing(
            "service",
            description="Kolo potrebuje servis vidlice a vymenu retezu.",
        )
        result = self.evaluate(item, market("service", quick=18000))
        self.assertEqual(result.status, "MANUAL_REVIEW")
        self.assertEqual(result.condition, "service_required")
        self.assertIn("service_required", result.flags)

    def test_completed_or_unnecessary_service_phrases_are_positive_condition(self) -> None:
        phrases = (
            "po servisu",
            "nově servisováno",
            "bez nutnosti servisu",
            "servis není potřeba",
            "bez dalších investic",
            "připraveno k jízdě",
        )
        for index, phrase in enumerate(phrases):
            with self.subTest(phrase=phrase):
                item = listing(
                    f"positive-condition-{index}",
                    description=f"Kolo je {phrase}.",
                )
                condition, flags = detect_condition(item, self.config)
                self.assertIn(condition, {"good", "excellent"})
                self.assertNotIn("service_required", flags)
                self.assertNotIn("condition_unknown", flags)

    def test_clear_czech_positive_condition_phrases_are_recognized(self) -> None:
        phrases = (
            "připravené k jízdě",
            "připravené na okamžité vyjetí",
            "ihned k použití",
            "stačí sednout a jet",
            "jen nasednout a jet",
            "plně funkční",
            "vše 100% funkční",
            "vše funguje na 100 %",
            "pravidelně servisované",
            "vše zavčasu servisováno",
            "v dobrém stavu",
            "v pěkném stavu",
            "v ideálním stavu",
            "v perfektním stavu",
            "ve výborném stavu",
            "bezvadný stav",
            "jako nové",
            "zachovalé",
            "udržované",
        )
        for index, phrase in enumerate(phrases):
            with self.subTest(phrase=phrase):
                item = listing(
                    f"clear-czech-condition-{index}",
                    description=f"Kolo je {phrase}.",
                )
                condition, flags = detect_condition(item, self.config)
                self.assertIn(condition, {"good", "excellent"})
                self.assertNotIn("condition_unknown", flags)

    def test_negated_positive_condition_phrases_are_not_accepted(self) -> None:
        phrases = (
            "není plně funkční",
            "není v dobrém stavu",
            "nezachovalé",
            "neudržované",
        )
        for index, phrase in enumerate(phrases):
            with self.subTest(phrase=phrase):
                item = listing(
                    f"negated-positive-condition-{index}",
                    description=f"Kolo je {phrase}.",
                )
                condition, flags = detect_condition(item, self.config)
                self.assertEqual(condition, "unknown")
                self.assertIn("condition_unknown", flags)

    def test_generic_marketing_phrases_do_not_prove_condition(self) -> None:
        item = listing(
            "marketing-only",
            description="Super kolo, skvělá nabídka, neváhejte.",
        )
        condition, flags = detect_condition(item, self.config)
        self.assertEqual(condition, "unknown")
        self.assertIn("condition_unknown", flags)

    def test_completed_shock_service_does_not_trigger_service_required(self) -> None:
        item = listing(
            "completed-shock-service",
            description=(
                "Dělal jsem servis tlumiče a vidlice 21.7.2026. "
                "Stav je dobrý a kolo je připraveno k jízdě."
            ),
        )
        condition, flags = detect_condition(item, self.config)
        self.assertEqual(condition, "good")
        self.assertNotIn("service_required", flags)

    def test_completed_service_does_not_hide_separate_required_service(self) -> None:
        item = listing(
            "mixed-service",
            description=(
                "Kolo je po servisu, ale nyní potřebuje servis vidlice."
            ),
        )
        condition, flags = detect_condition(item, self.config)
        self.assertEqual(condition, "contradictory")
        self.assertIn("service_required", flags)
        self.assertIn("condition_contradictory", flags)

    def test_negated_completed_service_is_not_treated_as_completed(self) -> None:
        item = listing(
            "not-serviced",
            description="Nedělal jsem servis tlumiče, je potřeba ho provést.",
        )
        condition, flags = detect_condition(item, self.config)
        self.assertEqual(condition, "service_required")
        self.assertIn("service_required", flags)

    def test_existing_manual_review_reason_blocks_hot(self) -> None:
        item = listing("existing-risk")
        item_analysis = analysis(item, market("existing-risk", quick=18000))
        item_analysis.risks.append("Цена подозрительно низкая относительно цены нового велосипеда.")
        result = self.evaluator.evaluate(item, item_analysis)
        self.assertEqual(result.status, "MANUAL_REVIEW")
        self.assertIn("stage_1_2_manual_risk", result.flags)

    def test_stale_batch_analysis_still_rejects_kids_bike(self) -> None:
        item = listing(
            "kids-bike",
            title="Dětské kolo Canyon Offspring AL 16",
            description="Kolo je po servisu a bez investic.",
        )
        stale_analysis = analysis(item, market("kids-bike", quick=18000))
        stale_analysis.identity.audience = "kids"
        stale_analysis.priority_class = "interesting_candidate"
        stale_analysis.notification_status = "awaiting_analysis"
        result = self.evaluator.evaluate(item, stale_analysis)
        self.assertEqual(result.status, "REJECT")
        self.assertIn("existing_hard_filter", result.flags)
        self.assertIn("hard_filter_kids_bike", result.flags)
        self.assertIsNotNone(result.net_profit_czk)

    def test_kids_bike_reject_is_not_selected_for_telegram(self) -> None:
        item = listing(
            "kids-telegram",
            title="Dětský Author Junior",
            description="Kolo je po servisu a bez investic.",
        )
        item_analysis = analysis(item, market("kids-telegram", quick=18000))
        item_analysis.identity.audience = "kids"
        item_analysis.deal_evaluation = self.evaluator.evaluate(item, item_analysis)
        selected = select_deal_notifications(
            [(item, item_analysis, 0.1)],
            self.config,
            max_cards=10,
            manual_review_reserved_slots=2,
            max_age_hours=6,
        )
        self.assertEqual(selected, [])

    def test_informational_market_warning_does_not_block_hot(self) -> None:
        item = listing("informational-warning")
        valuation = market("informational-warning", quick=18000)
        valuation.warnings = [
            "В Чехии подходящие аналоги не найдены; оценка построена по зарубежному рынку."
        ]
        result = self.evaluate(item, valuation)
        self.assertEqual(result.status, "HOT")
        self.assertNotIn("critical_market_warning", result.flags)

    def test_critical_market_warning_blocks_hot_for_manual_review(self) -> None:
        item = listing("critical-warning")
        valuation = market("critical-warning", quick=18000)
        valuation.warnings = ["Ambiguous model mismatch requires review."]
        result = self.evaluate(item, valuation)
        self.assertEqual(result.status, "MANUAL_REVIEW")
        self.assertIn("critical_market_warning", result.flags)

    def test_hot_financials_with_low_liquidity_are_interesting(self) -> None:
        valuation = market("slow", quick=18000, exact=1)
        result = self.evaluate(listing("slow"), valuation)
        self.assertEqual(result.liquidity_level, "low")
        self.assertEqual(result.status, "INTERESTING")
        self.assertIn("hot_blocked_by_liquidity", result.flags)

    def test_positive_weak_deal_is_low_priority(self) -> None:
        result = self.evaluate(listing("weak"), market("weak", median=14000, quick=12000))
        self.assertEqual(result.status, "LOW_PRIORITY")
        self.assertEqual(result.net_profit_czk, 1000)

    def test_slow_sale_below_hot_financials_is_low_priority(self) -> None:
        result = self.evaluate(
            listing("slow-weak", price=10000),
            market("slow-weak", median=16000, quick=13000, exact=1),
        )
        self.assertEqual(result.liquidity_level, "low")
        self.assertEqual(result.status, "LOW_PRIORITY")

    def test_missing_quick_sale_and_non_positive_total_are_manual(self) -> None:
        missing = self.evaluate(listing("missing"), market("missing", quick=None))
        invalid = self.evaluate(listing("zero-price", price=0), market("zero-price", quick=5000))
        self.assertEqual(missing.status, "MANUAL_REVIEW")
        self.assertIn("quick_sale_price_czk", missing.missing_fields)
        self.assertEqual(invalid.status, "MANUAL_REVIEW")
        self.assertIn("non_positive_total_investment", invalid.flags)

    def test_configuration_changes_status_without_code_change(self) -> None:
        item = listing("configurable")
        valuation = market("configurable", quick=16000)
        self.assertEqual(self.evaluate(item, valuation).status, "HOT")
        changed = DealScoringConfig(enabled=True, hot_min_profit_czk=7000, high_roi_percent=200)
        self.assertEqual(DealEvaluator(changed).evaluate(item, analysis(item, valuation)).status, "INTERESTING")

    def test_liquidity_comes_only_from_existing_stage_2_1_result(self) -> None:
        valuation = market(
            "liquidity",
            exact=2,
            close=2,
            countries=["CZ", "DE"],
            sources=["bazos_cz", "kleinanzeigen_de"],
        )
        score, level, days = calculate_liquidity(valuation, self.config)
        self.assertEqual(score, 60)
        self.assertEqual(level, "medium")
        self.assertEqual(days, self.config.estimated_days_medium)

    def test_cache_diagnostics_do_not_create_a_new_business_input(self) -> None:
        item = listing("cache-metadata")
        valuation = market("cache-metadata")
        first = self.evaluate(item, valuation)
        valuation.cache_used = True
        valuation.cache_hits = 3
        valuation.cache_misses = 2
        valuation.http_requests = {"bazos_cz": 1}
        second = self.evaluate(item, valuation)
        self.assertEqual(first.input_fingerprint, second.input_fingerprint)

    def test_batch_continues_after_one_evaluation_error(self) -> None:
        class OneFailureEvaluator(DealEvaluator):
            def evaluate(self, item, item_analysis, costs=None):
                if item.external_id == "bad":
                    raise RuntimeError("bad row")
                return super().evaluate(item, item_analysis, costs)

        good = listing("good")
        bad = listing("bad")
        evaluator = OneFailureEvaluator(self.config)
        results = evaluator.evaluate_batch(
            [
                (good, analysis(good, market("good")), None),
                (bad, analysis(bad, market("bad")), None),
            ]
        )
        self.assertEqual(results[0].status, "HOT")
        self.assertEqual(results[1].status, "MANUAL_REVIEW")
        self.assertIn("deal_evaluation_error", results[1].flags)


class DealEvaluationStorageTest(unittest.TestCase):
    def test_repeated_input_is_idempotent_but_changed_config_keeps_history(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "state.sqlite3"))
            item = listing("history")
            storage.register([item])
            item_analysis = analysis(item, market("history"))
            first = DealEvaluator(DealScoringConfig(enabled=True)).evaluate(item, item_analysis)
            self.assertTrue(storage.save_deal_evaluation(first))
            self.assertFalse(storage.save_deal_evaluation(first))
            changed = DealEvaluator(
                DealScoringConfig(enabled=True, hot_min_profit_czk=7000)
            ).evaluate(item, item_analysis)
            self.assertTrue(storage.save_deal_evaluation(changed))
            count = storage.connection.execute(
                "SELECT COUNT(*) FROM deal_evaluations WHERE listing_external_id = 'history'"
            ).fetchone()[0]
            latest = storage.get_latest_deal_evaluation("bazos", "history")
            storage.close()
            self.assertEqual(count, 2)
            self.assertEqual(latest.status if latest else None, "INTERESTING")

    def test_manual_cost_override_defaults_to_zero_and_roundtrips(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "state.sqlite3"))
            item = listing("costs")
            self.assertEqual(storage.get_deal_costs(item), DealCosts())
            storage.set_deal_costs(item, DealCosts(250, 500, 125))
            self.assertEqual(storage.get_deal_costs(item), DealCosts(250, 500, 125))
            storage.close()


class DealScoringConfigurationTest(unittest.TestCase):
    def test_environment_overrides_business_thresholds_and_existing_quick_sale_setting(self) -> None:
        raw = {
            "profiles": [
                {
                    "name": "test",
                    "rss_url": "https://sport.bazos.cz/rss.php?hledat=kolo",
                }
            ],
            "deal_scoring": {
                "enabled": True,
                "hot_min_profit_czk": 4000,
            },
            "market_pricing": {"enabled": True, "quick_sale_discount": 0.15},
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "DEAL_SCORING_ENABLED": "true",
                    "RISK_RESERVE_PERCENT": "12",
                    "QUICK_SALE_DISCOUNT_PERCENT": "20",
                    "HOT_MIN_PROFIT_CZK": "6500",
                    "TELEGRAM_SEND_REJECT": "true",
                },
                clear=False,
            ):
                config = load_config(path)
        self.assertEqual(config.deal_scoring.risk_reserve_percent, 12)
        self.assertEqual(config.deal_scoring.hot_min_profit_czk, 6500)
        self.assertTrue(config.deal_scoring.telegram_send_reject)
        self.assertEqual(config.market_pricing.quick_sale_discount, 0.20)

    def test_default_notification_policy_excludes_low_priority_and_reject(self) -> None:
        config = DealScoringConfig(enabled=True)
        items = []
        for index, status in enumerate(
            ("HOT", "INTERESTING", "MANUAL_REVIEW", "LOW_PRIORITY", "REJECT")
        ):
            item = listing(status.casefold())
            item_analysis = analysis(item, market(status.casefold()))
            result = DealEvaluator(config).evaluate(item, item_analysis)
            result.status = status
            item_analysis.deal_evaluation = result
            items.append((item, item_analysis, float(index)))
        selected = select_deal_notifications(
            items,
            config,
            max_cards=10,
            manual_review_reserved_slots=2,
            max_age_hours=10,
        )
        self.assertEqual(
            {item_analysis.deal_evaluation.status for _, item_analysis in selected},
            {"HOT", "INTERESTING", "MANUAL_REVIEW"},
        )


if __name__ == "__main__":
    unittest.main()
