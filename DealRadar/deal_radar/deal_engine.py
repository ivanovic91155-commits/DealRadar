"""Движок оценки сделок (DealScore).

Единственное место, где живёт ответ на вопрос «это сделка или мусор?».
Принимает объявление (Listing), опциональную оценку цены нового велосипеда
(Valuation) и идентичность (BikeIdentity), возвращает DealVerdict: балл 0-100,
тир (hot / notify / archive), разбивку по компонентам и понятные причины.

Формула (веса задаются в ScoringConfig):
    DealScore = 0.40·Маржа + 0.22·Ликвидность + 0.18·Риск
              + 0.10·Качество + 0.10·Свежесть

Маршрутизация:
    hot     — score >= hot_min  И  свежее (< hot_max_age)
    notify  — score >= notify_min
    archive — иначе (только в базу)

Свежесть входит в балл при условии частого опроса площадок (5-10 мин).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime

from deal_radar.models import BikeIdentity, Listing, Valuation
from deal_radar.scoring_config import ScoringConfig


# --- Результат ---------------------------------------------------------------

@dataclass(slots=True)
class ComponentScores:
    margin: float = 0.0
    liquidity: float = 0.0
    risk: float = 0.0
    quality: float = 0.0
    freshness: float = 0.0


@dataclass(slots=True)
class DealVerdict:
    score: int                       # итоговый DealScore 0-100
    tier: str                        # "hot" | "notify" | "archive"
    components: ComponentScores
    reasons: list[str] = field(default_factory=list)     # почему хорошо
    red_flags: list[str] = field(default_factory=list)   # что настораживает
    expected_resale_czk: int | None = None
    expected_profit_czk: int | None = None
    age_years: int | None = None
    freshness_minutes: float | None = None

    @property
    def is_hot(self) -> bool:
        return self.tier == "hot"

    @property
    def should_notify(self) -> bool:
        return self.tier in {"hot", "notify"}


# --- Утилиты -----------------------------------------------------------------

def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(plain.replace("-", " ").split())


def _interpolate(points: list[tuple[int, float]], x: float) -> float:
    """Кусочно-линейная интерполяция по возрастающим точкам (x, y).
    Ниже первой точки — y первой; выше последней — y последней.
    """
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            ratio = (x - x0) / (x1 - x0)
            return y0 + ratio * (y1 - y0)
    return points[-1][1]


def _age_bucket(model_year: int | None, now_year: int) -> tuple[str, int | None]:
    """Возвращает (ключ_для_age_factors, возраст_в_годах)."""
    if not model_year:
        return "unknown", None
    age = max(0, now_year - model_year)
    if age >= 5:
        return "5+", age
    return str(age), age


# --- Компоненты --------------------------------------------------------------

def score_margin(
    listing: Listing,
    identity: BikeIdentity,
    valuation: Valuation | None,
    config: ScoringConfig,
    now: datetime,
) -> tuple[float, int | None, int | None, list[str], list[str]]:
    """Возвращает (балл_0_100, ожидаемая_перепродажа, ожидаемая_прибыль,
    причины, флаги). Считает ожидаемую цену б/у от цены нового через
    амортизацию, вычитает цену объявления и издержки.
    """
    reasons: list[str] = []
    flags: list[str] = []

    if listing.price_czk is None:
        flags.append("цена в объявлении не распознана")
        return 0.0, None, None, reasons, flags

    # Источник цены нового: медиана из valuation, если есть успешная оценка.
    new_price = None
    if valuation is not None and valuation.median_price_czk:
        new_price = valuation.median_price_czk

    if not new_price:
        # Без цены нового маржу оценить нельзя — нейтральный ноль, не штраф.
        # Такое объявление всё равно может пройти по другим компонентам.
        return 0.0, None, None, reasons, flags

    factors = config.age_factors_ebike if identity.electric else config.age_factors
    bucket, age = _age_bucket(identity.model_year, now.year)
    factor = factors.get(bucket, factors.get("unknown", 0.5))

    expected_resale = int(round(new_price * factor))
    expected_profit = expected_resale - listing.price_czk - config.fixed_costs_czk

    score = _interpolate(config.margin_points, expected_profit)

    if expected_profit >= 2000:
        reasons.append(
            f"ожидаемая прибыль ≈ {expected_profit:,} Kč".replace(",", " ")
            + f" (перепродажа ~{expected_resale:,} Kč)".replace(",", " ")
        )
    elif expected_profit <= 0:
        flags.append("прибыль по расчёту не покрывает издержки")

    return score, expected_resale, expected_profit, reasons, flags


def score_liquidity(
    identity: BikeIdentity,
    config: ScoringConfig,
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    brand_key = _normalize(identity.brand)
    base = config.brand_liquidity.get(brand_key, config.brand_liquidity_fallback)
    if base >= 80 and identity.brand:
        reasons.append(f"ликвидный бренд ({identity.brand})")

    modifier = 0.0
    if identity.frame_size:
        modifier += config.frame_size_modifier.get(identity.frame_size.upper(), 0.0)
        if config.frame_size_modifier.get(identity.frame_size.upper(), 0.0) >= 10:
            reasons.append(f"ходовой размер рамы ({identity.frame_size})")
    if identity.bike_type:
        modifier += config.type_modifier.get(identity.bike_type, 0.0)

    score = max(0.0, min(100.0, base + modifier))
    return score, reasons


def score_risk(
    listing: Listing,
    config: ScoringConfig,
    expected_resale: int | None,
) -> tuple[float, list[str], bool]:
    """Возвращает (балл_0_100, флаги, есть_критический_флаг).

    Критический флаг (на запчасти / без документов) не просто снижает балл, но
    и ограничивает потолок тира до notify — такое объявление не может быть HOT,
    даже при высокой марже. Человек его увидит, но с пометкой «проверь вручную».
    Низкая цена сама по себе критическим флагом НЕ считается — для перекупа это
    сигнал сделки, а не риска; решение остаётся за человеком.
    """
    flags: list[str] = []
    critical = False
    score = 100.0
    r = config.risk
    text = _normalize(f"{listing.title} {listing.description}")

    if not listing.image_url:
        score -= r.no_photo
        flags.append("нет фото")

    word_count = len(listing.description.split())
    if word_count < r.short_description_word_threshold:
        score -= r.short_description
        flags.append("очень короткое описание")

    if listing.price_czk is None:
        score -= r.unrecognized_price

    if any(word in text for word in config.parts_nodocs_words):
        score -= r.parts_or_no_docs
        critical = True
        flags.append("признаки «на запчасти / без документов»")

    if any(word in text for word in config.damaged_words):
        score -= r.damaged
        flags.append("упомянуты повреждения/неисправность")

    return max(0.0, min(100.0, score)), flags, critical


def score_quality(
    listing: Listing,
    identity: BikeIdentity,
    config: ScoringConfig,
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    q = config.quality
    score = q.base

    if listing.image_url:
        score += q.has_photo
    word_count = len(listing.description.split())
    score += min(word_count, q.description_word_cap) * q.description_per_word
    if identity.frame_size:
        score += q.has_frame_size
    if identity.model_year:
        score += q.has_year

    return max(0.0, min(100.0, score)), reasons


def score_freshness(
    listing: Listing,
    config: ScoringConfig,
    now: datetime,
) -> tuple[float, float | None, list[str]]:
    reasons: list[str] = []
    if listing.published_at is None:
        # Без времени публикации свежесть неизвестна — нейтральная середина,
        # чтобы отсутствие данных не наказывало и не поощряло.
        return 50.0, None, reasons

    published = listing.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    age_minutes = (now - published).total_seconds() / 60.0
    age_minutes = max(0.0, age_minutes)

    f = config.freshness
    if age_minutes <= f.full_score_minutes:
        score = 100.0
        reasons.append("только что опубликовано — успеть первым")
    elif age_minutes >= f.zero_score_minutes:
        score = 0.0
    else:
        span = f.zero_score_minutes - f.full_score_minutes
        score = 100.0 * (1.0 - (age_minutes - f.full_score_minutes) / span)

    return score, age_minutes, reasons


# --- Главная функция ---------------------------------------------------------

def evaluate_deal(
    listing: Listing,
    identity: BikeIdentity,
    valuation: Valuation | None,
    config: ScoringConfig,
    now: datetime | None = None,
) -> DealVerdict:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    reasons: list[str] = []
    red_flags: list[str] = []

    margin_score, expected_resale, expected_profit, m_reasons, m_flags = score_margin(
        listing, identity, valuation, config, now
    )
    reasons.extend(m_reasons)
    red_flags.extend(m_flags)

    liquidity_score, l_reasons = score_liquidity(identity, config)
    reasons.extend(l_reasons)

    risk_score, risk_flags, has_critical = score_risk(listing, config, expected_resale)
    red_flags.extend(risk_flags)

    quality_score, q_reasons = score_quality(listing, identity, config)
    reasons.extend(q_reasons)

    freshness_score, fresh_minutes, f_reasons = score_freshness(listing, config, now)
    reasons.extend(f_reasons)

    components = ComponentScores(
        margin=round(margin_score, 1),
        liquidity=round(liquidity_score, 1),
        risk=round(risk_score, 1),
        quality=round(quality_score, 1),
        freshness=round(freshness_score, 1),
    )

    w = config.weights
    total = (
        margin_score * w.margin
        + liquidity_score * w.liquidity
        + risk_score * w.risk
        + quality_score * w.quality
        + freshness_score * w.freshness
    )
    score = int(round(max(0.0, min(100.0, total))))

    # Маршрутизация
    rt = config.routing
    is_fresh = fresh_minutes is not None and fresh_minutes <= rt.hot_max_age_minutes
    if score >= rt.hot_min_score and is_fresh and not has_critical:
        tier = "hot"
    elif score >= rt.notify_min_score:
        tier = "notify"
    else:
        tier = "archive"
    # Критический флаг (скам/запчасти) не даёт замолчать хорошее по баллам
    # объявление, но и не подаёт его как HOT — потолок notify.
    if has_critical and tier == "archive" and score >= rt.notify_min_score * 0.6:
        tier = "notify"

    _, age_years = _age_bucket(identity.model_year, now.year)

    return DealVerdict(
        score=score,
        tier=tier,
        components=components,
        reasons=reasons,
        red_flags=red_flags,
        expected_resale_czk=expected_resale,
        expected_profit_czk=expected_profit,
        age_years=age_years,
        freshness_minutes=fresh_minutes,
    )
