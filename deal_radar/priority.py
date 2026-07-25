from __future__ import annotations

import math
from datetime import UTC, datetime

from deal_radar.bike_identity import hard_filter_reason, identify_listing, normalize_text
from deal_radar.config import PriorityConfig
from deal_radar.models import BikeIdentity, Listing, ListingAnalysis, UsedComparables, Valuation
from deal_radar.pricing import discount_percent


PRIORITY_ORDER = {
    "urgent_candidate": 0,
    "interesting_candidate": 1,
    "manual_review": 2,
    "low_priority": 3,
    "excluded": 4,
}


def dynamic_lookup_budget(new_count: int, config: PriorityConfig | None = None) -> int:
    config = config or PriorityConfig()
    if new_count <= 0:
        return 0
    if new_count <= config.lookup_all_max_count:
        return new_count
    if new_count <= config.lookup_mid_max_count:
        return config.lookup_mid_limit
    if new_count <= config.lookup_high_max_count:
        return config.lookup_high_limit
    return min(
        config.lookup_max_limit,
        max(config.lookup_high_limit, math.ceil(new_count * config.lookup_large_fraction)),
    )


def priority_class(score: int, config: PriorityConfig, *, hard_reject: bool = False) -> str:
    if hard_reject or score <= 0:
        return "excluded"
    if score >= config.urgent_min_score:
        return "urgent_candidate"
    if score >= config.interesting_min_score:
        return "interesting_candidate"
    if score >= config.manual_review_min_score:
        return "manual_review"
    return "low_priority"


def _add(reasons: list[str], score: int, weight: int, reason: str) -> int:
    if weight:
        reasons.append(reason)
    return score + weight


def build_analysis(
    listing: Listing,
    config: PriorityConfig,
    *,
    identity: BikeIdentity | None = None,
    valuation: Valuation | None = None,
    used_comparables: UsedComparables | None = None,
    cache_used: bool = False,
    now: datetime | None = None,
) -> ListingAnalysis:
    identity = identity or identify_listing(listing)
    now = now or datetime.now(UTC)
    weights = config.weights
    reasons: list[str] = []
    risks: list[str] = []
    score = 0
    combined = normalize_text(f"{listing.title} {listing.description}")

    filter_reason = hard_filter_reason(listing, identity)
    hard_reject = bool(filter_reason)
    if hard_reject:
        risks.append(
            "Детский велосипед исключён политикой проекта."
            if filter_reason == "hard_filter_kids_bike"
            else "Объявление похоже на запчасть или комплектующую."
        )

    if listing.published_at:
        published = listing.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        age_seconds = (now - published.astimezone(UTC)).total_seconds()
        if -3600 <= age_seconds <= config.very_fresh_max_hours * 3600:
            score = _add(reasons, score, weights["very_fresh"], "Объявление опубликовано недавно.")
    else:
        risks.append("Время публикации не определено.")

    if identity.brand:
        score = _add(reasons, score, weights["brand_recognized"], "Бренд распознан.")
    else:
        risks.append("Бренд не подтверждён.")
    if identity.model:
        score = _add(reasons, score, weights["model_recognized"], "Модель распознана.")
    else:
        risks.append("Модель не подтверждена — требуется ручная проверка.")
    if identity.model_year:
        score = _add(reasons, score, weights["year_known"], "Год модели указан.")
    else:
        risks.append("Год модели неизвестен.")
    if identity.frame_size or identity.wheel_size:
        score = _add(reasons, score, weights["size_known"], "Размер велосипеда указан.")

    seller_price = listing.price_amount if listing.price_amount is not None else listing.price_czk
    if seller_price and seller_price > 0:
        score = _add(reasons, score, weights["numeric_price"], "Есть числовая цена продавца.")
    else:
        risks.append("Числовая цена продавца отсутствует.")
    if len(normalize_text(listing.description)) >= config.description_min_chars:
        score = _add(reasons, score, weights["description"], "Есть содержательное описание.")
    else:
        risks.append("Описание слишком короткое для уверенной оценки.")
    if listing.location or listing.profile:
        score = _add(reasons, score, weights["location_match"], "Локация соответствует профилю поиска.")

    if cache_used and valuation and valuation.median_price_czk:
        score = _add(reasons, score, weights["cached_new_price"], "Актуальная цена нового велосипеда найдена в кэше.")

    new_discount = discount_percent(seller_price, valuation.median_price_czk) if valuation else None
    if new_discount is not None:
        if new_discount >= 60:
            score = _add(reasons, score, weights["new_discount_60"], "Цена продавца на 60% или больше ниже цены нового велосипеда.")
        elif new_discount >= 45:
            score = _add(reasons, score, weights["new_discount_45"], "Цена продавца на 45–59% ниже цены нового велосипеда.")
        elif new_discount >= 30:
            score = _add(reasons, score, weights["new_discount_30"], "Цена продавца на 30–44% ниже цены нового велосипеда.")
        elif new_discount >= 15:
            score = _add(reasons, score, weights["new_discount_15"], "Цена продавца на 15–29% ниже цены нового велосипеда.")
        if new_discount >= config.suspicious_discount_percent:
            risks.append("Цена подозрительно низкая относительно цены нового велосипеда.")
    if valuation and valuation.independent_source_count == 1:
        risks.append("Цена нового велосипеда основана на одном источнике.")
    elif valuation and valuation.status not in {"success", "cached"}:
        risks.append("Подтверждённая цена нового велосипеда не найдена.")

    if used_comparables and used_comparables.median_price_czk and used_comparables.confidence in {"medium", "high"}:
        used_discount = discount_percent(seller_price, used_comparables.median_price_czk)
        if used_discount is not None and used_discount >= 30:
            score = _add(reasons, score, weights["used_discount_30"], "Цена на 30% или больше ниже медианы похожих б/у объявлений.")
        elif used_discount is not None and used_discount >= 15:
            score = _add(reasons, score, weights["used_discount_15"], "Цена на 15–29% ниже медианы похожих б/у объявлений.")
    elif used_comparables and 0 < used_comparables.count < 3:
        risks.append("Похожих б/у объявлений недостаточно для уверенного сравнения.")

    if any(term in combined for term in ("dohoda", "sleva", "negotiable", "торг")):
        score = _add(reasons, score, weights["negotiable"], "Продавец допускает торг.")
    if any(term in combined for term in ("specha", "urgent", "rychle", "срочно")):
        score = _add(reasons, score, weights["urgent_sale"], "Указана срочная продажа.")

    score = max(0, min(100, score))
    if not identity.model and not hard_reject and (seller_price or len(listing.description) >= config.description_min_chars):
        score = max(score, config.manual_review_min_score)
    classification = priority_class(score, config, hard_reject=hard_reject)
    if new_discount is not None and new_discount >= config.suspicious_discount_percent and not hard_reject:
        classification = "manual_review"

    if valuation and valuation.new_price_confidence in {"high", "medium"} and identity.confidence >= 0.8:
        analysis_confidence = "high"
    elif identity.brand and identity.model:
        analysis_confidence = "medium"
    else:
        analysis_confidence = "low"
    return ListingAnalysis(
        preliminary_priority_score=score,
        priority_class=classification,
        reasons=reasons,
        risks=risks,
        analysis_confidence=analysis_confidence,
        identity=identity,
        valuation=valuation,
        used_comparables=used_comparables,
        cache_used=cache_used,
        notification_status="excluded" if hard_reject else "awaiting_analysis",
        notification_reason=filter_reason,
    )


def select_lookup_candidates(
    items: list[tuple[Listing, ListingAnalysis]],
    budget: int,
) -> list[tuple[Listing, ListingAnalysis]]:
    eligible = [
        item
        for item in items
        if not item[1].cache_used
        and item[1].priority_class != "excluded"
        and item[1].identity is not None
        and bool(item[1].identity.brand and item[1].identity.model)
    ]
    return sorted(
        eligible,
        key=lambda item: (-item[1].preliminary_priority_score, item[0].key),
    )[: max(0, budget)]


def select_notifications(
    items: list[tuple[Listing, ListingAnalysis, float]],
    config: PriorityConfig,
) -> list[tuple[Listing, ListingAnalysis]]:
    current = [item for item in items if item[2] <= config.individual_notification_max_age_hours]
    primary = [item for item in current if item[1].priority_class in {"urgent_candidate", "interesting_candidate"}]
    manual = [item for item in current if item[1].priority_class == "manual_review"]
    primary.sort(key=lambda item: (PRIORITY_ORDER[item[1].priority_class], -item[1].preliminary_priority_score, item[0].key))
    manual.sort(key=lambda item: (-item[1].preliminary_priority_score, item[0].key))
    reserved = manual[: config.manual_review_reserved_slots]
    selected = primary[: config.max_telegram_cards - len(reserved)] + reserved
    selected.sort(key=lambda item: (PRIORITY_ORDER[item[1].priority_class], -item[1].preliminary_priority_score, item[0].key))
    return [(listing, analysis) for listing, analysis, _ in selected]
