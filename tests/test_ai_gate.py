"""AI Opportunity Gate и Telegram Notification Gate.

Сценарии A-G из ТЗ сессии 1 плюс проверки обратной совместимости: ворота не
должны ни терять качественный HOT, ни превращаться в жёсткий фильтр, который
выбрасывает плохо оформленные, но потенциально выгодные объявления.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from deal_radar.ai.listing_analysis import AnalysisOutcome, content_fingerprint
from deal_radar.ai_gate import (
    ACTION_BLOCKED,
    ACTION_DEEP_ANALYSIS,
    ACTION_STANDARD,
    BLOCK,
    SEND,
    SKIP,
    analysis_priority,
    notification_priority,
    read_signals,
    telegram_decision,
)
from deal_radar.config import (
    AIConfig,
    AIGateConfig,
    AppConfig,
    DealScoringConfig,
    MarketPricingConfig,
    RetailConfig,
    SearchProfile,
    TelegramConfig,
    TelegramGateConfig,
    load_config,
)
from deal_radar.deal_scoring import DealEvaluator, select_deal_notifications
from deal_radar.models import (
    AIAnalysis,
    AIClassification,
    AICondition,
    AIIdentity,
    AIOpportunity,
    AIRisk,
    AISpecifications,
    BikeIdentity,
    DealEvaluation,
    Listing,
    ListingAnalysis,
    MarketValuation,
)
from deal_radar.service import DealRadarService


def listing(external_id: str = "gate", *, price: int = 10000, title: str = "Trek Marlin 7 2024") -> Listing:
    return Listing(
        source="bazos",
        external_id=external_id,
        title=title,
        description="Kolo je po servisu, ve velmi dobrem stavu a bez investic.",
        url=f"https://bazos.example/{external_id}",
        profile="test",
        price_czk=price,
        price_amount=price,
        price_status="numeric",
        published_at=datetime.now(UTC),
    )


def ai_analysis(
    *,
    hidden_opportunity: bool = False,
    seller_urgency: str = "LOW",
    listing_quality: str = "MEDIUM",
    identity_confidence: float = 0.9,
    risk_flags: list[str] | None = None,
    possible_scam: bool = False,
    possible_stolen_bike: bool = False,
    claimed_condition: str = "GOOD",
    missing_parts: list[str] | None = None,
    is_electric: bool | None = False,
    brand: str | None = "Trek",
    model: str | None = "Marlin 7",
    model_year: int | None = 2024,
    bike_type: str | None = "MTB_HARDTAIL",
    specifications: bool = True,
    status: str = "AI_OK",
    is_bicycle: bool = True,
    listing_type: str = "COMPLETE_BICYCLE",
    relevance_confidence: float = 0.95,
) -> AIAnalysis:
    return AIAnalysis(
        status=status,
        classification=AIClassification(
            is_bicycle=is_bicycle,
            listing_type=listing_type,
            relevance_confidence=relevance_confidence,
        ),
        identity=AIIdentity(
            brand=brand,
            model=model,
            model_year=model_year,
            bike_type=bike_type,
            is_electric=is_electric,
            identity_confidence=identity_confidence,
        ),
        specifications=AISpecifications(
            frame_size_normalized="M" if specifications else None,
            wheel_size_inches=29.0 if specifications else None,
        ),
        condition=AICondition(
            claimed_condition=claimed_condition, missing_parts=missing_parts or []
        ),
        opportunity=AIOpportunity(
            seller_urgency=seller_urgency,
            listing_quality=listing_quality,
            hidden_opportunity=hidden_opportunity,
        ),
        risk=AIRisk(
            risk_flags=risk_flags or [],
            possible_scam=possible_scam,
            possible_stolen_bike=possible_stolen_bike,
        ),
    )


def bare_listing_analysis(**overrides: Any) -> AIAnalysis:
    """Разбор объявления «Prodám kolo» — модель, характеристики и состояние пусты."""

    settings: dict[str, Any] = {
        "hidden_opportunity": False,
        "seller_urgency": "LOW",
        "listing_quality": "LOW",
        "identity_confidence": 0.25,
        "risk_flags": ["VAGUE_DESCRIPTION"],
        "brand": None,
        "model": None,
        "model_year": None,
        "bike_type": None,
        "specifications": False,
        "claimed_condition": "UNKNOWN",
    }
    settings.update(overrides)
    return ai_analysis(**settings)


def deal(status: str = "MANUAL_REVIEW", **overrides: Any) -> DealEvaluation:
    fields: dict[str, Any] = {
        "listing_source": "bazos",
        "listing_external_id": "gate",
        "algorithm_version": "2.2.0",
        "status": status,
    }
    fields.update(overrides)
    return DealEvaluation(**fields)


def analysis(*, ai: AIAnalysis | None = None, evaluation: DealEvaluation | None = None) -> ListingAnalysis:
    return ListingAnalysis(
        preliminary_priority_score=40,
        priority_class="manual_review",
        identity=BikeIdentity(brand="Trek", model="Marlin 7", confidence=0.9),
        ai_analysis=ai,
        deal_evaluation=evaluation,
    )


def signals_for(ai: AIAnalysis | None, config: AIGateConfig | None = None, *, live: bool = True):
    config = config or AIGateConfig()
    return read_signals(analysis(ai=ai), config, live=live)


class AnalysisPriorityTest(unittest.TestCase):
    """Сценарии A-D: сколько внимания заслуживает объявление."""

    def setUp(self) -> None:
        self.config = AIGateConfig()

    def score(self, ai: AIAnalysis | None, *, live: bool = True):
        return analysis_priority(signals_for(ai, self.config, live=live), self.config)

    def test_scenario_a_ordinary_bad_listing_gets_low_priority(self) -> None:
        # «Prodám kolo, 8 000 Kč»: ни модели, ни характеристик, ни причины.
        decision = self.score(bare_listing_analysis())
        self.assertLess(decision.score, self.config.analysis_min_score)
        self.assertEqual(decision.action, ACTION_STANDARD)
        self.assertIn("identity_unknown:-5", decision.reasons)
        self.assertIn("risk_minor:-5", decision.reasons)
        self.assertNotIn("valuable_information:+5", decision.reasons)

    def test_scenario_b_bad_but_promising_listing_is_prioritized(self) -> None:
        # «Scott kolo, 5000, model nevím, spěchá»: то же качество объявления,
        # что и в сценарии A, но у него есть причина.
        promising = self.score(
            bare_listing_analysis(
                hidden_opportunity=True,
                seller_urgency="HIGH",
                identity_confidence=0.40,
                brand="Scott",
                risk_flags=[],
            )
        )
        ordinary = self.score(bare_listing_analysis(identity_confidence=0.40, risk_flags=[]))
        self.assertEqual(promising.action, ACTION_DEEP_ANALYSIS)
        self.assertEqual(ordinary.action, ACTION_STANDARD)
        self.assertGreater(promising.score, ordinary.score)
        self.assertIn("hidden_opportunity:+30", promising.reasons)
        self.assertIn("seller_urgency_high:+20", promising.reasons)

    def test_low_listing_quality_alone_is_not_a_penalty(self) -> None:
        low = self.score(ai_analysis(listing_quality="LOW"))
        medium = self.score(ai_analysis(listing_quality="MEDIUM"))
        difference = medium.score - low.score
        self.assertEqual(difference, self.config.listing_quality_bonus["MEDIUM"])
        self.assertLessEqual(difference, self.config.hidden_opportunity_bonus // 2)
        self.assertFalse([reason for reason in low.reasons if reason.startswith("listing_quality")])

    def test_scenario_c_clean_ordinary_listing_still_deserves_a_market_check(self) -> None:
        decision = self.score(
            ai_analysis(listing_quality="HIGH", identity_confidence=0.95, seller_urgency="LOW")
        )
        self.assertEqual(decision.action, ACTION_DEEP_ANALYSIS)
        self.assertIn("identity_high:+10", decision.reasons)

    def test_scenario_d_risk_penalty_lowers_the_score(self) -> None:
        clean = self.score(ai_analysis(hidden_opportunity=True, seller_urgency="HIGH"))
        risky = self.score(
            ai_analysis(
                hidden_opportunity=True,
                seller_urgency="HIGH",
                risk_flags=["ADVANCE_PAYMENT_REQUESTED"],
            )
        )
        self.assertEqual(
            clean.score - risky.score, self.config.risk_penalty["severe"]
        )
        self.assertIn("risk_severe:-40", risky.reasons)

    def test_blocking_risk_stops_deep_analysis(self) -> None:
        decision = self.score(ai_analysis(hidden_opportunity=True, possible_scam=True))
        self.assertEqual(decision.action, ACTION_BLOCKED)

    def test_a_non_bicycle_is_blocking_at_any_confidence(self) -> None:
        # Жёсткое подавление в сервисе требует уверенности ≥ 0.75. Шлем,
        # опознанный на 0.6, обязан остаться в базе, но не дойти до человека.
        decision = self.score(
            ai_analysis(
                is_bicycle=False, listing_type="ACCESSORY", relevance_confidence=0.6
            )
        )
        self.assertEqual(decision.action, ACTION_BLOCKED)

    def test_a_vague_other_listing_is_not_blocked(self) -> None:
        # OTHER слишком размыт: спорный случай должен дойти до человека.
        decision = self.score(
            ai_analysis(is_bicycle=False, listing_type="OTHER", relevance_confidence=0.9)
        )
        self.assertNotEqual(decision.action, ACTION_BLOCKED)

    def test_missing_analysis_keeps_the_listing_in_the_middle(self) -> None:
        for label, ai in (
            ("no analysis", None),
            ("failed", ai_analysis(status="AI_FAILED")),
            ("pending", ai_analysis(status="AI_PENDING")),
        ):
            with self.subTest(label=label):
                decision = self.score(ai)
                self.assertEqual(decision.score, self.config.base_score)
                self.assertIn("ai_unavailable:+0", decision.reasons)

    def test_shadow_mode_keeps_ai_signals_out_of_the_gate(self) -> None:
        loud = ai_analysis(hidden_opportunity=True, seller_urgency="HIGH")
        self.assertEqual(self.score(loud, live=False).score, self.config.base_score)
        self.assertGreater(self.score(loud).score, self.config.base_score)

    def test_disabled_gate_scores_nothing(self) -> None:
        config = AIGateConfig(enabled=False)
        decision = analysis_priority(signals_for(ai_analysis(), config), config)
        self.assertEqual(decision.score, 0)
        self.assertEqual(decision.reasons, [])


class NotificationPriorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = TelegramGateConfig()

    def decide(self, evaluation: DealEvaluation, ai: AIAnalysis | None = None, *, live: bool = True):
        item = analysis(ai=ai, evaluation=evaluation)
        return telegram_decision(item, signals_for(ai, live=live), self.config)

    def hot(self) -> DealEvaluation:
        return deal(
            "HOT",
            purchase_price_czk=10000.0,
            market_median_czk=20000.0,
            net_profit_czk=6000.0,
            roi_percent=54.0,
            liquidity_score=70,
            confidence_score=85,
            deal_score=82.0,
        )

    def test_scenario_e_real_hot_is_never_lost(self) -> None:
        decision = self.decide(self.hot())
        self.assertEqual(decision.action, SEND)
        self.assertEqual(decision.reason, "status_hot")
        self.assertGreaterEqual(decision.score, self.config.manual_review_min_score)

    def test_hot_survives_without_any_ai_signal(self) -> None:
        decision = self.decide(self.hot(), None, live=False)
        self.assertEqual(decision.action, SEND)

    def test_scenario_d_blocking_risk_stops_even_a_hot_card(self) -> None:
        decision = self.decide(self.hot(), ai_analysis(possible_stolen_bike=True))
        self.assertEqual(decision.action, BLOCK)
        self.assertEqual(decision.reason, "blocking_risk_stolen_risk")

    def test_a_non_bicycle_never_reaches_telegram(self) -> None:
        # Шлем POC Axion: жёсткое подавление его пропустило (уверенность 0.6),
        # ворота — нет.
        decision = self.decide(
            deal("MANUAL_REVIEW"),
            ai_analysis(
                is_bicycle=False,
                listing_type="ACCESSORY",
                relevance_confidence=0.6,
                seller_urgency="HIGH",
            ),
        )
        self.assertEqual(decision.action, BLOCK)
        self.assertEqual(decision.reason, "blocking_risk_not_a_bicycle")

    def test_scenario_f_manual_review_without_a_signal_is_not_sent(self) -> None:
        decision = self.decide(
            deal("MANUAL_REVIEW", deal_score=40.0),
            ai_analysis(hidden_opportunity=False, seller_urgency="LOW", listing_quality="LOW"),
        )
        self.assertEqual(decision.action, SKIP)
        self.assertEqual(decision.reason, "no_strong_signal")

    def test_scenario_g_manual_review_with_hidden_opportunity_is_sent(self) -> None:
        decision = self.decide(
            deal("MANUAL_REVIEW", deal_score=40.0),
            ai_analysis(hidden_opportunity=True, listing_quality="LOW", identity_confidence=0.4),
        )
        self.assertEqual(decision.action, SEND)
        self.assertEqual(decision.reason, "hidden_opportunity")
        self.assertIn("hidden_opportunity:+20", decision.reasons)

    def test_urgent_seller_rescues_a_manual_review(self) -> None:
        decision = self.decide(
            deal("MANUAL_REVIEW"), ai_analysis(seller_urgency="HIGH", listing_quality="LOW")
        )
        self.assertEqual(decision.action, SEND)
        self.assertEqual(decision.reason, "seller_urgency_high")

    def test_price_anomaly_rescues_a_manual_review_without_any_ai(self) -> None:
        decision = self.decide(
            deal("MANUAL_REVIEW", purchase_price_czk=6000.0, market_median_czk=20000.0),
            None,
            live=False,
        )
        self.assertEqual(decision.action, SEND)
        self.assertEqual(decision.reason, "price_anomaly")

    def test_profitable_manual_review_passes_on_score_alone(self) -> None:
        decision = self.decide(
            deal(
                "MANUAL_REVIEW",
                purchase_price_czk=14000.0,
                market_median_czk=18000.0,
                net_profit_czk=9000.0,
                roi_percent=64.0,
                liquidity_score=60,
                confidence_score=65,
            ),
            None,
            live=False,
        )
        self.assertEqual(decision.action, SEND)
        self.assertEqual(decision.reason, "notification_score")

    def test_scenario_c_good_identification_alone_is_not_interesting(self) -> None:
        decision = self.decide(
            deal("MANUAL_REVIEW"),
            ai_analysis(listing_quality="HIGH", identity_confidence=0.95, seller_urgency="LOW"),
        )
        self.assertEqual(decision.action, SKIP)
        self.assertLess(decision.score, self.config.manual_review_min_score)

    def test_interesting_needs_the_notification_threshold(self) -> None:
        strong = self.decide(
            deal("INTERESTING", net_profit_czk=5000.0, roi_percent=30.0, confidence_score=65)
        )
        weak = self.decide(
            deal("INTERESTING"),
            ai_analysis(risk_flags=["ADVANCE_PAYMENT_REQUESTED"]),
        )
        self.assertEqual(strong.action, SEND)
        self.assertEqual(weak.action, SKIP)
        self.assertEqual(weak.reason, "score_below_interesting_threshold")

    def test_severe_risk_lowers_the_notification_score(self) -> None:
        evaluation = self.hot()
        clean = notification_priority(analysis(evaluation=evaluation), signals_for(None), self.config)
        risky = notification_priority(
            analysis(evaluation=evaluation),
            signals_for(ai_analysis(risk_flags=["ADVANCE_PAYMENT_REQUESTED"])),
            self.config,
        )
        self.assertLess(risky.score, clean.score)

    def test_low_priority_and_reject_stay_with_the_existing_send_policy(self) -> None:
        for status in ("LOW_PRIORITY", "REJECT"):
            with self.subTest(status=status):
                decision = self.decide(deal(status))
                self.assertEqual(decision.action, SEND)
                self.assertEqual(decision.reason, "status_send_policy")

    def test_disabled_gate_sends_everything_as_before(self) -> None:
        self.config = TelegramGateConfig(enabled=False)
        decision = self.decide(deal("MANUAL_REVIEW"))
        self.assertEqual(decision.action, SEND)
        self.assertEqual(decision.reason, "gate_disabled")

    def test_without_strong_signal_rule_only_the_score_decides(self) -> None:
        self.config = TelegramGateConfig(manual_review_require_strong_signal=False)
        decision = self.decide(deal("MANUAL_REVIEW"), ai_analysis(hidden_opportunity=True))
        self.assertEqual(decision.action, SKIP)
        self.assertEqual(decision.reason, "score_below_manual_review_threshold")


class NotificationOrderTest(unittest.TestCase):
    def test_notification_score_orders_cards_inside_a_status(self) -> None:
        config = DealScoringConfig(enabled=True)
        items = []
        for external_id, score in (("quiet", 20), ("loud", 80)):
            item = listing(external_id)
            item_analysis = analysis(evaluation=deal("MANUAL_REVIEW", deal_score=50.0))
            item_analysis.notification_priority_score = score
            items.append((item, item_analysis, 0.5))
        selected = select_deal_notifications(
            items,
            config,
            max_cards=10,
            manual_review_reserved_slots=2,
            max_age_hours=6,
        )
        self.assertEqual([item.external_id for item, _ in selected], ["loud", "quiet"])

    def test_hot_still_outranks_a_loud_manual_review(self) -> None:
        config = DealScoringConfig(enabled=True)
        hot_analysis = analysis(evaluation=deal("HOT", deal_score=70.0))
        hot_analysis.notification_priority_score = 60
        manual_analysis = analysis(evaluation=deal("MANUAL_REVIEW", deal_score=50.0))
        manual_analysis.notification_priority_score = 95
        selected = select_deal_notifications(
            [
                (listing("manual"), manual_analysis, 0.5),
                (listing("hot"), hot_analysis, 0.5),
            ],
            config,
            max_cards=10,
            manual_review_reserved_slots=2,
            max_age_hours=6,
        )
        self.assertEqual([item.external_id for item, _ in selected], ["hot", "manual"])


class GateConfigurationTest(unittest.TestCase):
    def test_environment_overrides_the_gate_thresholds(self) -> None:
        import os
        from unittest.mock import patch

        raw = {
            "profiles": [{"name": "test", "rss_url": "https://sport.bazos.cz/rss.php?hledat=kolo"}],
            "ai_opportunity_gate": {"hidden_opportunity_bonus": 25},
            "telegram_notification_gate": {"interesting_min_score": 30},
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "TELEGRAM_MANUAL_REVIEW_MIN_SCORE": "75",
                    "AI_GATE_ANALYSIS_MIN_SCORE": "40",
                    "TELEGRAM_MANUAL_REVIEW_REQUIRE_STRONG_SIGNAL": "false",
                },
                clear=False,
            ):
                config = load_config(path)
        self.assertEqual(config.telegram_gate.manual_review_min_score, 75)
        self.assertEqual(config.telegram_gate.interesting_min_score, 30)
        self.assertFalse(config.telegram_gate.manual_review_require_strong_signal)
        self.assertEqual(config.ai_gate.analysis_min_score, 40)
        self.assertEqual(config.ai_gate.hidden_opportunity_bonus, 25)

    def test_defaults_are_enabled_and_valid(self) -> None:
        config = AppConfig(
            profiles=[SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")]
        )
        config.validate()
        self.assertTrue(config.ai_gate.enabled)
        self.assertTrue(config.telegram_gate.enabled)
        self.assertFalse(config.ai_signals_live)

    def test_unknown_strong_signal_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TelegramGateConfig(strong_signals=["vibes"]).validate()

    def test_unknown_risk_severity_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AIGateConfig(risk_severity={"PRICE_TOO_LOW": "catastrophic"}).validate()


class StubAnalyzer:
    """Level 1 без сети: заранее заданный разбор для каждого объявления."""

    class Prompt:
        name = "listing-analysis"
        version = "v1.2.0"
        schema_version = "1.0.0"

    def __init__(self, answers: dict[str, AIAnalysis]) -> None:
        self.answers = answers
        self.prompt = self.Prompt()

    def _prepare(self, item: Listing, source: AIAnalysis) -> AIAnalysis:
        prepared = AIAnalysis.from_dict(source.to_dict())
        prepared.prompt_name = self.prompt.name
        prepared.prompt_version = self.prompt.version
        prepared.schema_version = self.prompt.schema_version
        prepared.model_name = "stub"
        prepared.content_hash = content_fingerprint(item)
        return prepared

    def analyze(self, item: Listing, identity=None) -> AnalysisOutcome:
        return AnalysisOutcome(analysis=self._prepare(item, self.answers[item.external_id]))

    def skipped(self, item: Listing, prefilter) -> AIAnalysis:
        return AIAnalysis(status="AI_SKIPPED", content_hash=content_fingerprint(item))

    def pending(self, item: Listing, reason_code: str) -> AIAnalysis:
        return AIAnalysis(status="AI_PENDING", content_hash=content_fingerprint(item))


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


class GateCycleTest(unittest.TestCase):
    """Полный цикл: воронка с воротами вместо «слот свободен — отправляем»."""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def build(
        self,
        items: list[Listing],
        *,
        ai: AIConfig | None = None,
        telegram_gate: TelegramGateConfig | None = None,
        market_pricing: MarketPricingConfig | None = None,
    ) -> tuple[DealRadarService, FakeTelegram]:
        config = AppConfig(
            database_path=str(Path(self.directory.name) / "state.sqlite3"),
            bootstrap_mode="send_all",
            profiles=[SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")],
            telegram=TelegramConfig(bot_token="test", chat_id="1"),
            retail=RetailConfig(enabled=False),
            market_pricing=market_pricing or MarketPricingConfig(enabled=False),
            deal_scoring=DealScoringConfig(enabled=True),
            ai=ai or AIConfig(),
            telegram_gate=telegram_gate or TelegramGateConfig(),
        )
        config.validate()
        service = DealRadarService(config)
        service.sources = [FakeSource(items)]
        return service, FakeTelegram()

    def test_plain_manual_review_is_no_longer_sent_but_stays_in_the_database(self) -> None:
        service, telegram = self.build([listing("plain")])
        try:
            stats = service.process_once(telegram)
            stored = service.storage.get_analysis("bazos", "plain")
        finally:
            service.close()
        self.assertEqual(stats["deal_manual_review"], 1)
        self.assertEqual(telegram.sent, [])
        self.assertEqual(stats["manual_review_suppressed"], 1)
        self.assertEqual(stats["manual_review_sent"], 0)
        assert stored is not None
        self.assertEqual(stored.notification_status, "not_selected")
        self.assertEqual(stored.notification_reason, "telegram_gate_no_strong_signal")
        self.assertTrue(stored.notification_reasons)

    def test_hidden_opportunity_lets_a_manual_review_through(self) -> None:
        item = listing("hidden", title="Scott kolo", price=5000)
        item.description = "Spěchá, model nevím."
        service, telegram = self.build(
            [item],
            ai=AIConfig(enabled=True, api_key="sk-test", shadow_mode=False),
        )
        service._ai_analyzer = lambda: StubAnalyzer(  # type: ignore[method-assign]
            {"hidden": ai_analysis(hidden_opportunity=True, seller_urgency="HIGH", listing_quality="LOW")}
        )
        try:
            stats = service.process_once(telegram)
            stored = service.storage.get_analysis("bazos", "hidden")
        finally:
            service.close()
        self.assertEqual(telegram.sent, ["hidden"])
        self.assertEqual(stats["manual_review_sent"], 1)
        self.assertEqual(stats["manual_review_suppressed"], 0)
        assert stored is not None
        self.assertEqual(stored.telegram_gate_reason, "hidden_opportunity")
        self.assertEqual(stored.ai_gate_action, ACTION_DEEP_ANALYSIS)
        self.assertGreater(stored.analysis_priority_score, 50)
        self.assertIn("hidden_opportunity:+30", stored.analysis_priority_reasons)

    def test_shadow_mode_keeps_the_ai_signals_unused(self) -> None:
        item = listing("shadow", title="Scott kolo", price=5000)
        service, telegram = self.build(
            [item],
            ai=AIConfig(enabled=True, api_key="sk-test", shadow_mode=True),
        )
        service._ai_analyzer = lambda: StubAnalyzer(  # type: ignore[method-assign]
            {"shadow": ai_analysis(hidden_opportunity=True, seller_urgency="HIGH")}
        )
        try:
            service.process_once(telegram)
            stored = service.storage.get_analysis("bazos", "shadow")
        finally:
            service.close()
        self.assertEqual(telegram.sent, [])
        assert stored is not None and stored.ai_analysis is not None
        self.assertEqual(stored.ai_analysis.status, "AI_OK")
        self.assertIn("ai_unavailable:+0", stored.analysis_priority_reasons)

    def test_hot_still_reaches_telegram_with_the_gate_on(self) -> None:
        class MarketFinder:
            def find(self, item, identity, new_valuation=None):
                return MarketValuation(
                    listing_source=item.source,
                    listing_external_id=item.external_id,
                    market_price_czk=20000,
                    quick_sale_price_czk=17000,
                    price_low_czk=18000,
                    price_high_czk=22000,
                    confidence="medium",
                    valuation_method="weighted_median",
                    status="market_price_found",
                    comparables_total=3,
                    comparables_unique=3,
                    exact_comparables=3,
                    cz_comparables=3,
                    countries_used=["CZ"],
                    sources_used=["bazos_cz"],
                )

        service, telegram = self.build(
            [listing("hot")], market_pricing=MarketPricingConfig(enabled=True)
        )
        service._market_finder = lambda: MarketFinder()  # type: ignore[method-assign]
        try:
            stats = service.process_once(telegram)
            stored = service.storage.get_analysis("bazos", "hot")
        finally:
            service.close()
        self.assertEqual(telegram.sent, ["hot"])
        self.assertEqual(stats["hot_sent"], 1)
        assert stored is not None
        self.assertEqual(stored.telegram_gate_action, SEND)
        self.assertGreater(stored.notification_priority_score, 60)

    def test_a_helmet_never_costs_an_api_call_and_never_reaches_telegram(self) -> None:
        # Тот самый POC Axion из жалобы владельца: чешское слово «helma» не
        # знал дешёвый фильтр, и шлем доезжал до карточки «🚲 POC Axion».
        item = listing("helma", title="Helma POC Axion 55-58", price=1599)
        service, telegram = self.build(
            [item], ai=AIConfig(enabled=True, api_key="sk-test", shadow_mode=False)
        )

        class ForbiddenAnalyzer(StubAnalyzer):
            def analyze(self, item: Listing, identity=None):
                raise AssertionError("a helmet must not cost an AI call")

        service._ai_analyzer = lambda: ForbiddenAnalyzer({})  # type: ignore[method-assign]
        try:
            service.process_once(telegram)
            stored = service.storage.get_analysis("bazos", "helma")
        finally:
            service.close()
        self.assertEqual(telegram.sent, [])
        assert stored is not None and stored.ai_analysis is not None
        self.assertEqual(stored.ai_analysis.status, "AI_SKIPPED")
        self.assertEqual(stored.notification_status, "excluded")
        self.assertEqual(stored.priority_class, "excluded")

    def test_a_bare_accessory_title_is_stopped_by_the_ai_verdict(self) -> None:
        # «POC Axion» без существительного дешёвый фильтр опознать не может —
        # предмет называет только фотография, и решает уже AI.
        item = listing("bare", title="POC Axion", price=1599)
        service, telegram = self.build(
            [item], ai=AIConfig(enabled=True, api_key="sk-test", shadow_mode=False)
        )
        service._ai_analyzer = lambda: StubAnalyzer(  # type: ignore[method-assign]
            {
                "bare": ai_analysis(
                    is_bicycle=False,
                    listing_type="ACCESSORY",
                    relevance_confidence=0.6,
                    brand="POC",
                    model="Axion",
                )
            }
        )
        try:
            service.process_once(telegram)
            stored = service.storage.get_analysis("bazos", "bare")
        finally:
            service.close()
        self.assertEqual(telegram.sent, [])
        assert stored is not None
        self.assertEqual(stored.telegram_gate_action, BLOCK)
        self.assertEqual(stored.notification_reason, "telegram_gate_blocking_risk_not_a_bicycle")

    def test_a_confident_non_bicycle_is_removed_before_the_gate(self) -> None:
        item = listing("sure", title="POC Axion", price=1599)
        service, telegram = self.build(
            [item], ai=AIConfig(enabled=True, api_key="sk-test", shadow_mode=False)
        )
        service._ai_analyzer = lambda: StubAnalyzer(  # type: ignore[method-assign]
            {
                "sure": ai_analysis(
                    is_bicycle=False, listing_type="ACCESSORY", relevance_confidence=0.95
                )
            }
        )
        try:
            service.process_once(telegram)
            stored = service.storage.get_analysis("bazos", "sure")
        finally:
            service.close()
        self.assertEqual(telegram.sent, [])
        assert stored is not None
        self.assertEqual(stored.notification_reason, "ai_not_a_bicycle")

    def test_disabled_gate_restores_the_previous_behaviour(self) -> None:
        service, telegram = self.build(
            [listing("legacy")], telegram_gate=TelegramGateConfig(enabled=False)
        )
        try:
            stats = service.process_once(telegram)
        finally:
            service.close()
        self.assertEqual(telegram.sent, ["legacy"])
        self.assertEqual(stats["manual_review_sent"], 1)


class LegacyAnalysisCompatibilityTest(unittest.TestCase):
    def test_analysis_saved_before_the_gate_still_loads(self) -> None:
        with TemporaryDirectory() as directory:
            from deal_radar.storage import Storage

            storage = Storage(str(Path(directory) / "state.sqlite3"))
            item = listing("legacy")
            storage.register([item])
            stored = analysis(evaluation=deal("MANUAL_REVIEW"))
            payload = stored.to_dict()
            for key in (
                "analysis_priority_score",
                "analysis_priority_reasons",
                "ai_gate_action",
                "notification_priority_score",
                "notification_reasons",
                "telegram_gate_action",
                "telegram_gate_reason",
            ):
                payload.pop(key)
            storage.connection.execute(
                "UPDATE listings SET analysis_json = ? WHERE source = ? AND external_id = ?",
                (json.dumps(payload, ensure_ascii=False), item.source, item.external_id),
            )
            storage.connection.commit()
            loaded = storage.get_analysis("bazos", "legacy")
            storage.close()
        assert loaded is not None
        self.assertEqual(loaded.analysis_priority_score, 0)
        self.assertEqual(loaded.notification_reasons, [])
        self.assertEqual(loaded.telegram_gate_action, "")


class DealEvaluatorUnchangedTest(unittest.TestCase):
    def test_the_gate_does_not_touch_deal_status_or_money(self) -> None:
        item = listing("unchanged")
        item_analysis = analysis(ai=ai_analysis(hidden_opportunity=True, seller_urgency="HIGH"))
        item_analysis.market_valuation = MarketValuation(
            listing_source="bazos",
            listing_external_id="unchanged",
            market_price_czk=19000,
            quick_sale_price_czk=16000,
            confidence="medium",
            valuation_method="weighted_median",
            status="market_price_found",
            comparables_total=3,
            comparables_unique=3,
            exact_comparables=3,
            cz_comparables=3,
            countries_used=["CZ"],
            sources_used=["bazos_cz"],
        )
        evaluation = DealEvaluator(DealScoringConfig(enabled=True)).evaluate(item, item_analysis)
        self.assertEqual(evaluation.status, "HOT")
        self.assertEqual(evaluation.net_profit_czk, 5000)


if __name__ == "__main__":
    unittest.main()
