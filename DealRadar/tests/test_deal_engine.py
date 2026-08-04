from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from deal_radar.deal_engine import evaluate_deal, _interpolate
from deal_radar.models import BikeIdentity, Listing, Valuation
from deal_radar.scoring_config import ScoringConfig


NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def make_listing(
    *,
    title="Trek Marlin 7 2024",
    description="Prodám horské kolo Trek Marlin 7, velikost L, málo jeté, super stav, veškeré doklady k dispozici, pěkný stav rámu i komponentů.",
    price_czk=9000,
    image_url="https://example.com/photo.jpg",
    published_minutes_ago=5,
) -> Listing:
    published = NOW - timedelta(minutes=published_minutes_ago) if published_minutes_ago is not None else None
    return Listing(
        source="bazos",
        external_id="123456",
        title=title,
        description=description,
        url="https://www.bazos.cz/inzerat/123456/kolo.php",
        profile="test",
        price_czk=price_czk,
        location="Praha 5, 150 00",
        published_at=published,
        image_url=image_url,
    )


def make_identity(
    *,
    brand="Trek",
    model="Marlin 7",
    model_year=2024,
    frame_size="L",
    bike_type="mountain",
    electric=None,
    confidence=0.9,
) -> BikeIdentity:
    return BikeIdentity(
        brand=brand,
        model=model,
        model_year=model_year,
        frame_size=frame_size,
        bike_type=bike_type,
        electric=electric,
        confidence=confidence,
    )


def make_valuation(median_price_czk=18000) -> Valuation:
    return Valuation(
        identified_product="Trek Marlin 7 2024",
        confidence="high",
        median_price_czk=median_price_czk,
        status="success",
        source_count=4,
    )


class InterpolationTest(unittest.TestCase):
    def test_margin_points_curve(self):
        points = [(0, 0.0), (2000, 60.0), (4000, 80.0), (8000, 95.0), (12000, 100.0)]
        self.assertEqual(_interpolate(points, -500), 0.0)
        self.assertEqual(_interpolate(points, 0), 0.0)
        self.assertEqual(_interpolate(points, 2000), 60.0)
        self.assertEqual(_interpolate(points, 3000), 70.0)  # середина 2000-4000
        self.assertEqual(_interpolate(points, 4000), 80.0)
        self.assertEqual(_interpolate(points, 12000), 100.0)
        self.assertEqual(_interpolate(points, 50000), 100.0)  # выше потолка


class DealEngineTest(unittest.TestCase):
    def setUp(self):
        self.config = ScoringConfig()
        self.config.validate()

    def test_modest_deal_passes(self):
        """Скромная сделка (+~3000 Kč) должна уверенно проходить в notify."""
        # new 18000 * 0.68 (Trek, 2 года) = 12240; -9000 -1500 = +1740 прибыль
        listing = make_listing(price_czk=9000)
        identity = make_identity(model_year=2024)
        verdict = evaluate_deal(listing, identity, make_valuation(18000), self.config, now=NOW)
        self.assertGreaterEqual(verdict.score, 40)
        self.assertIn(verdict.tier, {"notify", "hot"})
        self.assertTrue(verdict.should_notify)

    def test_fat_deal_scores_high_and_hot(self):
        """Жирная свежая сделка → высокий балл и tier=hot."""
        # 2024 при now=2026 → возраст 2 года → factor 0.63 (после смягчения)
        # new 25000 * 0.63 = 15750; цена 8000 (выше скам-порога 45%=7087)
        # прибыль 15750-8000-1500 = +6250 → маржа ~91, честная сделка
        listing = make_listing(price_czk=8000, published_minutes_ago=5)
        identity = make_identity(model_year=2024)
        verdict = evaluate_deal(listing, identity, make_valuation(25000), self.config, now=NOW)
        self.assertGreaterEqual(verdict.score, 70)
        self.assertEqual(verdict.tier, "hot")
        self.assertGreater(verdict.expected_profit_czk, 5000)

    def test_low_price_no_longer_flagged(self):
        """Низкая цена больше НЕ создаёт скам-флаг и не мешает высокому баллу.
        Для перекупа дешевизна — сигнал сделки, решение за человеком."""
        listing = make_listing(price_czk=4000, published_minutes_ago=3)
        identity = make_identity(model_year=2024)
        verdict = evaluate_deal(listing, identity, make_valuation(25000), self.config, now=NOW)
        self.assertFalse(
            any("подозрительно" in flag for flag in verdict.red_flags),
            verdict.red_flags,
        )
        # очень дешёвый выгодный свежий велосипед теперь может быть HOT
        self.assertEqual(verdict.tier, "hot")

    def test_parts_only_cannot_be_hot(self):
        """«На запчасти» — критический флаг, потолок notify."""
        listing = make_listing(
            title="Trek 2024",
            description="Prodám na díly, bez dokladů, jinak super. Krásné kolo Trek Marlin velikost L.",
            price_czk=6000,
            published_minutes_ago=3,
        )
        identity = make_identity(model_year=2024)
        verdict = evaluate_deal(listing, identity, make_valuation(25000), self.config, now=NOW)
        self.assertNotEqual(verdict.tier, "hot")

    def test_no_profit_low_margin(self):
        """Нет маржи (цена ≈ рынок) → низкий балл маржи."""
        # new 12000 * 0.68 = 8160; -8000 -1500 = -1340 (убыток)
        listing = make_listing(price_czk=8000)
        identity = make_identity(model_year=2024)
        verdict = evaluate_deal(listing, identity, make_valuation(12000), self.config, now=NOW)
        self.assertEqual(verdict.components.margin, 0.0)

    def test_no_photo_penalizes_risk_and_quality(self):
        listing = make_listing(image_url=None)
        identity = make_identity()
        verdict = evaluate_deal(listing, identity, make_valuation(18000), self.config, now=NOW)
        self.assertTrue(any("нет фото" in flag for flag in verdict.red_flags))
        self.assertLess(verdict.components.risk, 100)

    def test_parts_only_heavy_penalty(self):
        listing = make_listing(
            description="Prodám pouze díly z kola, na díly, bez dokladů. Rám, vidlice.",
        )
        identity = make_identity()
        verdict = evaluate_deal(listing, identity, make_valuation(18000), self.config, now=NOW)
        self.assertTrue(
            any("запчасти" in flag for flag in verdict.red_flags), verdict.red_flags
        )
        self.assertLessEqual(verdict.components.risk, 50)

    def test_freshness_boosts_fresh_listing(self):
        fresh = make_listing(published_minutes_ago=3)
        stale = make_listing(published_minutes_ago=240)
        identity = make_identity()
        v_fresh = evaluate_deal(fresh, identity, make_valuation(18000), self.config, now=NOW)
        v_stale = evaluate_deal(stale, identity, make_valuation(18000), self.config, now=NOW)
        self.assertEqual(v_fresh.components.freshness, 100.0)
        self.assertEqual(v_stale.components.freshness, 0.0)
        self.assertGreater(v_fresh.score, v_stale.score)

    def test_stale_good_deal_not_hot_but_notifies(self):
        """Старое, но выгодное объявление: не hot (не свежее), но notify."""
        listing = make_listing(price_czk=7000, published_minutes_ago=200)
        identity = make_identity()
        verdict = evaluate_deal(listing, identity, make_valuation(25000), self.config, now=NOW)
        self.assertNotEqual(verdict.tier, "hot")
        self.assertTrue(verdict.should_notify)

    def test_below_notify_threshold_archived(self):
        """Слабое объявление (<40) уходит только в базу."""
        # маленькая маржа + нишевый бренд + нет фото + короткое описание
        listing = make_listing(
            title="Neznámé kolo",
            description="kolo",
            price_czk=11500,
            image_url=None,
        )
        identity = make_identity(brand="NoName", model="xyz", model_year=2020, frame_size="XS", bike_type="")
        verdict = evaluate_deal(listing, identity, make_valuation(12000), self.config, now=NOW)
        self.assertLess(verdict.score, 40)
        self.assertEqual(verdict.tier, "archive")
        self.assertFalse(verdict.should_notify)

    def test_unrecognized_price_not_dropped(self):
        """Нераспознанная цена → флаг, но не выбрасывание (маржа=0, но объявление живёт)."""
        listing = make_listing(price_czk=None)
        identity = make_identity()
        verdict = evaluate_deal(listing, identity, make_valuation(18000), self.config, now=NOW)
        self.assertTrue(any("не распознана" in flag for flag in verdict.red_flags))
        self.assertIsNotNone(verdict.score)

    def test_no_valuation_still_scores(self):
        """Без цены нового маржа=0, но ликвидность/качество/свежесть работают."""
        listing = make_listing()
        identity = make_identity()
        verdict = evaluate_deal(listing, identity, None, self.config, now=NOW)
        self.assertEqual(verdict.components.margin, 0.0)
        self.assertGreater(verdict.components.liquidity, 0)
        self.assertGreater(verdict.components.quality, 0)

    def test_ebike_uses_steeper_depreciation(self):
        """E-bike амортизируется быстрее → ниже ожидаемая перепродажа."""
        listing = make_listing(price_czk=9000)
        regular = make_identity(electric=None, model_year=2024)
        ebike = make_identity(electric=True, model_year=2024)
        v_regular = evaluate_deal(listing, regular, make_valuation(18000), self.config, now=NOW)
        v_ebike = evaluate_deal(listing, ebike, make_valuation(18000), self.config, now=NOW)
        self.assertLess(v_ebike.expected_resale_czk, v_regular.expected_resale_czk)

    def test_liquid_brand_beats_niche(self):
        listing = make_listing()
        trek = make_identity(brand="Trek")
        niche = make_identity(brand="ObscureBrand")
        v_trek = evaluate_deal(listing, trek, make_valuation(18000), self.config, now=NOW)
        v_niche = evaluate_deal(listing, niche, make_valuation(18000), self.config, now=NOW)
        self.assertGreater(v_trek.components.liquidity, v_niche.components.liquidity)


class ConfigTest(unittest.TestCase):
    def test_weights_must_sum_to_one(self):
        config = ScoringConfig()
        config.weights.margin = 0.9  # ломаем сумму
        with self.assertRaises(ValueError):
            config.validate()

    def test_from_dict_overrides_defaults(self):
        config = ScoringConfig.from_dict({"fixed_costs_czk": 2500})
        self.assertEqual(config.fixed_costs_czk, 2500)
        # остальное — дефолты
        self.assertEqual(config.weights.margin, 0.40)

    def test_from_dict_empty_is_valid(self):
        config = ScoringConfig.from_dict(None)
        self.assertEqual(config.fixed_costs_czk, 1500)

    def test_routing_thresholds_override(self):
        config = ScoringConfig.from_dict({"routing": {"notify_min_score": 50}})
        self.assertEqual(config.routing.notify_min_score, 50.0)


if __name__ == "__main__":
    unittest.main()
