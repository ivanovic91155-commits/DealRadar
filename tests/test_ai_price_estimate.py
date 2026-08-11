"""AI Analysis Level 2: оценка цены перепродажи.

Главное здесь — не то, что оценка появляется, а то, что она не появляется
там, где ответ модели неправдоподобен, и что догадка не превращается в HOT.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from deal_radar.ai.client import OpenAIClient
from deal_radar.ai.price_estimate import (
    PriceEstimator,
    condition_bucket,
    estimate_key,
    needs_estimate,
    price_cost_usd,
    to_market_valuation,
)
from deal_radar.config import (
    AIConfig,
    AppConfig,
    DealScoringConfig,
    MarketPricingConfig,
    RetailConfig,
    SearchProfile,
    TelegramConfig,
)
from deal_radar.deal_scoring import DealEvaluator
from deal_radar.models import (
    AIAnalysis,
    AICondition,
    AIIdentity,
    AISpecifications,
    BikeIdentity,
    DealCosts,
    Listing,
    ListingAnalysis,
    MarketValuation,
)
from deal_radar.service import DealRadarService

ANSWER: dict[str, Any] = {
    "market_price_czk": 12000,
    "price_low_czk": 10000,
    "price_high_czk": 14000,
    "confidence": "medium",
    "basis": "SAME_MODEL",
    "reasoning_summary": "Trek Marlin 7 2022 in good condition, mid-range hardtail.",
    "price_drivers": [
        {"factor": "Shimano Deore groupset", "direction": "RAISES", "excerpt": "Shimano Deore"}
    ],
    "warnings": [],
}


def api_response(payload: dict[str, Any] | None = None, **usage: int) -> dict[str, Any]:
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(payload if payload is not None else ANSWER),
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": usage.get("input_tokens", 1800),
            "input_tokens_details": {"cached_tokens": usage.get("cached_tokens", 0)},
            "output_tokens": usage.get("output_tokens", 400),
        },
    }


class RecordingPoster:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses) or [api_response()]
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url, payload, headers, timeout=90):
        self.calls.append(payload)
        item = self.responses[min(len(self.calls), len(self.responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item


def listing(external_id: str = "1", price_czk: int | None = 9000, source: str = "bazos") -> Listing:
    return Listing(
        source=source,
        external_id=external_id,
        profile="test",
        title="Trek Marlin 7 2022",
        url=f"https://sport.bazos.cz/inzerat/{external_id}/bike.php",
        description="Kolo po servisu, Shimano Deore, ram M.",
        price_czk=price_czk,
        price_amount=price_czk,
        price_status="numeric",
        published_at=datetime.now(UTC),
    )


def level_one(**condition: Any) -> AIAnalysis:
    return AIAnalysis(
        status="AI_OK",
        identity=AIIdentity(brand="Trek", model="Marlin 7", model_year=2022),
        specifications=AISpecifications(frame_size_normalized="M", wheel_size_inches=29),
        condition=AICondition(**({"claimed_condition": "GOOD"} | condition)),
    )


def estimator(poster: RecordingPoster, **overrides: Any) -> PriceEstimator:
    settings: dict[str, Any] = {
        "api_key": "sk-test",
        "enabled": True,
        "price_estimate_enabled": True,
    }
    settings.update(overrides)
    config = AIConfig(**settings)
    config.validate()
    return PriceEstimator(
        config, client=OpenAIClient(config, poster=poster, sleeper=lambda _: None)
    )


class RequestTest(unittest.TestCase):
    def test_the_pricing_model_is_used_not_the_extraction_model(self) -> None:
        poster = RecordingPoster()
        estimator(poster).estimate(listing(), None, level_one())
        self.assertEqual(poster.calls[0]["model"], "gpt-5.6-terra")

    def test_partial_market_data_is_offered_as_an_anchor(self) -> None:
        poster = RecordingPoster()
        instance = estimator(poster)
        market = MarketValuation(
            listing_source="bazos", listing_external_id="1", comparables_unique=1, status="market_price_low_confidence"
        )
        payload = instance.build_payload(listing(), None, level_one(), market, None)
        self.assertEqual(payload["partial_market_data"]["engine_status"], "market_price_low_confidence")
        self.assertEqual(payload["partial_market_data"]["comparables_found"], 1)
        self.assertEqual(payload["listing"]["asking_price_czk"], 9000)

    def test_condition_and_specs_from_level_one_are_passed_on(self) -> None:
        instance = estimator(RecordingPoster())
        payload = instance.build_payload(listing(), None, level_one(), None, None)
        self.assertEqual(payload["specifications"]["frame_size_normalized"], "M")
        self.assertEqual(payload["condition"]["claimed_condition"], "GOOD")

    def test_seller_contacts_never_reach_the_request(self) -> None:
        poster = RecordingPoster()
        item = listing()
        item.description = "Tel 776123456, mail me@example.cz"
        estimator(poster).estimate(item, None, level_one())
        body = str(poster.calls[0])
        self.assertNotIn("776123456", body)
        self.assertNotIn("me@example.cz", body)


class EstimateTest(unittest.TestCase):
    def test_valid_answer_is_accepted_with_its_range(self) -> None:
        estimate = estimator(RecordingPoster()).estimate(listing(), None, level_one()).estimate
        self.assertEqual(estimate.status, "PRICE_OK")
        self.assertEqual(estimate.market_price_czk, 12000)
        self.assertEqual((estimate.price_low_czk, estimate.price_high_czk), (10000, 14000))
        self.assertEqual(estimate.confidence, "medium")
        self.assertEqual(estimate.basis, "SAME_MODEL")
        self.assertGreater(estimate.estimated_cost_usd, 0)

    def test_cost_uses_the_pricing_model_rates(self) -> None:
        config = AIConfig()
        expected = price_cost_usd(
            config, used_fallback=False, input_tokens=1800, cached_input_tokens=0, output_tokens=400
        )
        outcome = estimator(RecordingPoster()).estimate(listing(), None, level_one())
        self.assertAlmostEqual(outcome.estimate.estimated_cost_usd, expected, places=8)
        # Terra дороже Luna: расход не должен считаться по дешёвому прайсу.
        self.assertGreater(expected, 0.001)

    def test_api_failure_becomes_price_failed_without_raising(self) -> None:
        from deal_radar.http import HttpError

        outcome = estimator(RecordingPoster(HttpError("HTTP 500", 500))).estimate(
            listing(), None, level_one()
        )
        self.assertEqual(outcome.estimate.status, "PRICE_FAILED")
        assert outcome.call_log is not None
        self.assertEqual(outcome.call_log["success"], 0)

    def test_swapped_range_bounds_are_repaired(self) -> None:
        answer = {**ANSWER, "price_low_czk": 14000, "price_high_czk": 10000}
        estimate = estimator(RecordingPoster(api_response(answer))).estimate(
            listing(), None, level_one()
        ).estimate
        self.assertEqual(estimate.status, "PRICE_OK")
        self.assertLessEqual(estimate.price_low_czk, estimate.market_price_czk)
        self.assertGreaterEqual(estimate.price_high_czk, estimate.market_price_czk)

    def test_vague_basis_caps_the_confidence_the_model_claims(self) -> None:
        answer = {**ANSWER, "basis": "GENERIC", "confidence": "high"}
        estimate = estimator(RecordingPoster(api_response(answer))).estimate(
            listing(), None, level_one()
        ).estimate
        self.assertEqual(estimate.confidence, "low")


class SanityGuardTest(unittest.TestCase):
    """Structured Outputs гарантирует форму ответа, но не его смысл."""

    def reject(self, **changes: Any) -> str:
        answer = {**ANSWER, **changes}
        estimate = estimator(RecordingPoster(api_response(answer))).estimate(
            listing(price_czk=9000), None, level_one()
        ).estimate
        return estimate.reject_reason if estimate.status == "PRICE_REJECTED" else ""

    def test_price_far_above_the_asking_price_is_rejected(self) -> None:
        self.assertEqual(
            self.reject(market_price_czk=200000, price_low_czk=190000, price_high_czk=210000),
            "implausibly_above_asking",
        )

    def test_price_far_below_the_asking_price_is_rejected(self) -> None:
        self.assertEqual(
            self.reject(market_price_czk=600, price_low_czk=550, price_high_czk=650),
            "implausibly_below_asking",
        )

    def test_price_below_the_absolute_floor_is_rejected(self) -> None:
        self.assertEqual(
            self.reject(market_price_czk=100, price_low_czk=90, price_high_czk=110),
            "outside_absolute_bounds",
        )

    def test_a_range_too_wide_to_be_useful_is_rejected(self) -> None:
        self.assertEqual(
            self.reject(market_price_czk=12000, price_low_czk=4000, price_high_czk=30000),
            "range_too_wide",
        )

    def test_a_rejected_estimate_never_becomes_a_valuation(self) -> None:
        answer = {**ANSWER, "market_price_czk": 200000, "price_low_czk": 190000, "price_high_czk": 210000}
        estimate = estimator(RecordingPoster(api_response(answer))).estimate(
            listing(), None, level_one()
        ).estimate
        self.assertIsNone(to_market_valuation(estimate, listing(), 0.15))

    def test_a_sound_estimate_becomes_a_valuation_with_its_own_status(self) -> None:
        estimate = estimator(RecordingPoster()).estimate(listing(), None, level_one()).estimate
        valuation = to_market_valuation(estimate, listing(), 0.15)
        assert valuation is not None
        self.assertEqual(valuation.status, "ai_estimate")
        self.assertEqual(valuation.market_price_czk, 12000)
        # Скидка быстрой продажи берётся из конфига проекта, а не у модели.
        self.assertEqual(valuation.quick_sale_price_czk, 10200)


class CacheKeyTest(unittest.TestCase):
    def test_same_bike_in_different_ads_shares_a_key(self) -> None:
        identity = BikeIdentity(brand="Trek", model="Marlin 7", model_year=2022, wheel_size="29")
        first, kind = estimate_key(listing("a"), identity, level_one())
        second, _ = estimate_key(listing("b"), identity, level_one())
        self.assertEqual(first, second)
        self.assertEqual(kind, "identity")

    def test_condition_separates_the_key(self) -> None:
        identity = BikeIdentity(brand="Trek", model="Marlin 7", model_year=2022)
        good, _ = estimate_key(listing("a"), identity, level_one())
        broken, _ = estimate_key(listing("a"), identity, level_one(defects=["bent rim"]))
        self.assertNotEqual(good, broken)

    def test_year_and_wheel_size_separate_the_key(self) -> None:
        base = BikeIdentity(brand="Trek", model="Marlin 7", model_year=2022, wheel_size="29")
        older = BikeIdentity(brand="Trek", model="Marlin 7", model_year=2016, wheel_size="29")
        small = BikeIdentity(brand="Trek", model="Marlin 7", model_year=2022, wheel_size="27.5")
        keys = {estimate_key(listing(), item, level_one())[0] for item in (base, older, small)}
        self.assertEqual(len(keys), 3)

    def test_unknown_model_falls_back_to_a_per_listing_key(self) -> None:
        # Иначе один ответ про «непонятный велосипед» разошёлся бы по всем
        # неопознанным объявлениям сразу.
        vague = BikeIdentity(brand="", model="")
        first, kind = estimate_key(listing("a"), vague, None)
        second, _ = estimate_key(listing("b"), vague, None)
        self.assertNotEqual(first, second)
        self.assertEqual(kind, "listing")

    def test_condition_bucket_marks_defects_and_service(self) -> None:
        self.assertEqual(condition_bucket(level_one()), "GOOD")
        self.assertEqual(condition_bucket(level_one(service_needed=True)), "GOOD_ISSUES")
        self.assertEqual(condition_bucket(None), "unknown")


class TriggerTest(unittest.TestCase):
    def valuation(self, **fields: Any) -> MarketValuation:
        defaults = {
            "listing_source": "bazos",
            "listing_external_id": "1",
            "market_price_czk": 15000,
            "comparables_unique": 5,
            "confidence": "medium",
        }
        return MarketValuation(**(defaults | fields))

    def test_no_valuation_at_all_triggers_the_estimate(self) -> None:
        self.assertTrue(needs_estimate(None, AIConfig()))

    def test_a_valuation_without_a_price_triggers_the_estimate(self) -> None:
        self.assertTrue(needs_estimate(self.valuation(market_price_czk=None), AIConfig()))

    def test_too_few_comparables_triggers_the_estimate(self) -> None:
        self.assertTrue(needs_estimate(self.valuation(comparables_unique=1), AIConfig()))

    def test_low_confidence_triggers_the_estimate(self) -> None:
        self.assertTrue(needs_estimate(self.valuation(confidence="low"), AIConfig()))

    def test_a_solid_valuation_is_left_alone(self) -> None:
        self.assertFalse(needs_estimate(self.valuation(), AIConfig()))


class HotGuardTest(unittest.TestCase):
    """Догадка модели не должна становиться сигналом «ехать покупать»."""

    def evaluate(self, status: str, **config_fields: Any):
        config = DealScoringConfig(enabled=True, **config_fields)
        analysis = ListingAnalysis(
            preliminary_priority_score=80,
            priority_class="urgent_candidate",
            identity=BikeIdentity(
                brand="Trek", model="Marlin 7", confidence=0.9, model_confirmed=True
            ),
            market_valuation=MarketValuation(
                listing_source="bazos",
                listing_external_id="1",
                market_price_czk=40000,
                quick_sale_price_czk=34000,
                confidence="high",
                status=status,
                comparables_unique=6,
                exact_comparables=6,
                cz_comparables=6,
                countries_used=["CZ"],
                sources_used=["bazos_cz", "kleinanzeigen_de"],
            ),
        )
        return DealEvaluator(config).evaluate(listing(price_czk=9000), analysis, DealCosts())

    def test_measured_comparables_can_still_produce_hot(self) -> None:
        self.assertEqual(self.evaluate("market_price_found").status, "HOT")

    def test_an_ai_estimate_is_capped_at_interesting(self) -> None:
        result = self.evaluate("ai_estimate")
        self.assertEqual(result.status, "INTERESTING")
        self.assertIn("hot_blocked_by_ai_price", result.flags)

    def test_the_cap_can_be_lifted_deliberately(self) -> None:
        self.assertEqual(self.evaluate("ai_estimate", allow_hot_on_ai_price=True).status, "HOT")


class FakeSource:
    def __init__(self, listings: list[Listing]) -> None:
        self.listings = listings
        self.profile = SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")

    def fetch(self) -> list[Listing]:
        return self.listings


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_listing(self, item: Listing, retail_enabled: bool, analysis=None) -> int:
        self.sent.append(item.external_id)
        return 100 + len(self.sent)

    def poll_feedback(self, offset: int = 0):
        return [], offset


class CyclePhaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def build(self, items: list[Listing], **ai_fields: Any) -> tuple[DealRadarService, FakeTelegram]:
        settings: dict[str, Any] = {
            "enabled": True,
            "api_key": "sk-test",
            "price_estimate_enabled": True,
        }
        settings.update(ai_fields)
        config = AppConfig(
            database_path=str(Path(self.directory.name) / "state.sqlite3"),
            bootstrap_mode="send_all",
            profiles=[SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")],
            telegram=TelegramConfig(bot_token="test", chat_id="1"),
            retail=RetailConfig(enabled=False),
            market_pricing=MarketPricingConfig(enabled=False),
            deal_scoring=DealScoringConfig(enabled=True),
            ai=AIConfig(**settings),
        )
        config.validate()
        service = DealRadarService(config)
        service.sources = [FakeSource(items)]
        # Level 1 не участвует: проверяется именно ценовая фаза.
        service._ai_analyzer = lambda: None  # type: ignore[method-assign]
        return service, FakeTelegram()

    def install(self, service: DealRadarService, poster: RecordingPoster) -> None:
        instance = estimator(poster)
        service._price_estimator = lambda: instance  # type: ignore[method-assign]

    def test_disabled_price_phase_leaves_the_funnel_untouched(self) -> None:
        service, telegram = self.build([listing("off")], price_estimate_enabled=False)
        try:
            stats = service.process_once(telegram)
        finally:
            service.close()
        self.assertFalse([key for key in stats if key.startswith("ai_price_")])

    def test_estimate_fills_an_empty_valuation(self) -> None:
        service, telegram = self.build([listing("fill")])
        poster = RecordingPoster()
        self.install(service, poster)
        try:
            stats = service.process_once(telegram)
            stored = service.storage.get_analysis("bazos", "fill")
        finally:
            service.close()
        self.assertEqual(stats["ai_price_calls"], 1)
        self.assertEqual(stats["ai_price_applied"], 1)
        assert stored is not None and stored.market_valuation is not None
        self.assertEqual(stored.market_valuation.status, "ai_estimate")
        self.assertEqual(stored.market_valuation.market_price_czk, 12000)
        assert stored.ai_price is not None
        self.assertEqual(stored.ai_price.status, "PRICE_OK")

    def test_a_rejected_estimate_leaves_the_price_empty(self) -> None:
        answer = {**ANSWER, "market_price_czk": 300000, "price_low_czk": 290000, "price_high_czk": 310000}
        service, telegram = self.build([listing("bad")])
        self.install(service, RecordingPoster(api_response(answer)))
        try:
            stats = service.process_once(telegram)
            stored = service.storage.get_analysis("bazos", "bad")
        finally:
            service.close()
        self.assertEqual(stats["ai_price_rejected"], 1)
        self.assertEqual(stats["ai_price_applied"], 0)
        assert stored is not None
        self.assertIsNone(stored.market_valuation)

    def test_identical_bikes_cost_a_single_call(self) -> None:
        items = [listing("a"), listing("b"), listing("c")]
        service, telegram = self.build(items)
        poster = RecordingPoster()
        self.install(service, poster)
        try:
            stats = service.process_once(telegram)
        finally:
            service.close()
        # Один и тот же велосипед в трёх объявлениях — один платный вызов.
        self.assertEqual(len(poster.calls), 1)
        self.assertEqual(stats["ai_price_cache_hits"], 2)
        self.assertEqual(stats["ai_price_applied"], 3)

    def test_exhausted_budget_leaves_listings_pending(self) -> None:
        service, telegram = self.build([listing("broke")], daily_budget_usd=0.001)
        poster = RecordingPoster()
        self.install(service, poster)
        service.storage.log_ai_call(
            {
                "request_id": "spent",
                "started_at": datetime.now(UTC).isoformat(),
                "estimated_total_cost_usd": 0.05,
            }
        )
        try:
            stats = service.process_once(telegram)
        finally:
            service.close()
        self.assertEqual(poster.calls, [])
        self.assertEqual(stats["ai_price_pending"], 1)

    def test_api_outage_keeps_the_cycle_alive(self) -> None:
        from deal_radar.http import HttpError

        service, telegram = self.build([listing("outage")])
        self.install(service, RecordingPoster(HttpError("HTTP 503", 503)))
        try:
            stats = service.process_once(telegram)
        finally:
            service.close()
        self.assertEqual(stats["new"], 1)
        self.assertEqual(stats["ai_price_failed"], 1)
        self.assertEqual(telegram.sent, ["outage"])


if __name__ == "__main__":
    unittest.main()
