from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import asdict
from decimal import Decimal, ROUND_HALF_UP

from deal_radar.bike_identity import has_accessory_terms, normalize_text
from deal_radar.config import DealScoringConfig
from deal_radar.models import DealCosts, DealEvaluation, Listing, ListingAnalysis, MarketValuation


LOGGER = logging.getLogger(__name__)
VALID_STATUSES = {"HOT", "INTERESTING", "MANUAL_REVIEW", "LOW_PRIORITY", "REJECT"}
DEAL_STATUS_ORDER = {
    "HOT": 0,
    "INTERESTING": 1,
    "MANUAL_REVIEW": 2,
    "LOW_PRIORITY": 3,
    "REJECT": 4,
}


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _contains_any(text: str, terms: list[str]) -> bool:
    return any(normalize_text(term) in text for term in terms if normalize_text(term))


def detect_condition(listing: Listing, config: DealScoringConfig) -> tuple[str, list[str]]:
    text = normalize_text(f"{listing.title} {listing.description}")
    has_positive = _contains_any(text, config.positive_condition_terms)
    has_service = _contains_any(text, config.service_required_terms)
    has_problem = _contains_any(text, config.problem_condition_terms)
    flags: list[str] = []
    if has_service:
        flags.append("service_required")
    if has_problem:
        flags.append("condition_problem")
    if has_positive and (has_service or has_problem):
        return "contradictory", [*flags, "condition_contradictory"]
    if has_problem:
        return "problematic", flags
    if has_service:
        return "service_required", flags
    if has_positive:
        excellent_terms = ("top stav", "perfektni stav", "vyborny stav", "fully serviced")
        return ("excellent" if any(term in text for term in excellent_terms) else "good"), flags
    return "unknown", ["condition_unknown"]


def market_scope(valuation: MarketValuation | None) -> str:
    if valuation is None or not valuation.countries_used:
        return "unknown"
    countries = set(valuation.countries_used)
    if countries == {"CZ"}:
        return "cz_only"
    if "CZ" in countries:
        return "mixed"
    return "foreign_only"


def calculate_liquidity(
    valuation: MarketValuation | None,
    config: DealScoringConfig,
) -> tuple[int | None, str, int | None]:
    if valuation is None or valuation.comparables_unique <= 0:
        return None, "unknown", None
    score = (
        valuation.exact_comparables * config.liquidity_exact_points
        + valuation.close_comparables * config.liquidity_close_points
        + valuation.family_comparables * config.liquidity_family_points
        + valuation.component_comparables * config.liquidity_component_points
        + min(len(set(valuation.sources_used)), config.liquidity_source_count_cap)
        * config.liquidity_source_points
        + (config.liquidity_czech_bonus if valuation.cz_comparables else 0)
    )
    score = max(0, min(100, int(round(score))))
    if score >= config.liquidity_high_min_score:
        return score, "high", config.estimated_days_high
    if score >= config.liquidity_medium_min_score:
        return score, "medium", config.estimated_days_medium
    return score, "low", config.estimated_days_low


def select_deal_notifications(
    items: list[tuple[Listing, ListingAnalysis, float]],
    config: DealScoringConfig,
    *,
    max_cards: int,
    manual_review_reserved_slots: int,
    max_age_hours: int,
) -> list[tuple[Listing, ListingAnalysis]]:
    enabled = {
        "HOT": config.telegram_send_hot,
        "INTERESTING": config.telegram_send_interesting,
        "MANUAL_REVIEW": config.telegram_send_manual_review,
        "LOW_PRIORITY": config.telegram_send_low_priority,
        "REJECT": config.telegram_send_reject,
    }
    current = [
        item
        for item in items
        if item[2] <= max_age_hours
        and item[1].deal_evaluation is not None
        and enabled.get(item[1].deal_evaluation.status, False)
    ]

    def key(item: tuple[Listing, ListingAnalysis, float]) -> tuple[int, float, str]:
        evaluation = item[1].deal_evaluation
        assert evaluation is not None
        return DEAL_STATUS_ORDER[evaluation.status], -evaluation.deal_score, item[0].key

    manual = sorted(
        [item for item in current if item[1].deal_evaluation.status == "MANUAL_REVIEW"],
        key=key,
    )
    others = sorted(
        [item for item in current if item[1].deal_evaluation.status != "MANUAL_REVIEW"],
        key=key,
    )
    reserved = manual[: min(manual_review_reserved_slots, max_cards)]
    selected = others[: max(0, max_cards - len(reserved))] + reserved
    if len(selected) < max_cards:
        selected_keys = {item[0].key for item in selected}
        remainder = sorted(
            [item for item in current if item[0].key not in selected_keys],
            key=key,
        )
        selected.extend(remainder[: max_cards - len(selected)])
    return [(listing, analysis) for listing, analysis, _ in sorted(selected, key=key)]


def _purchase_price_czk(
    listing: Listing,
    valuation: MarketValuation | None,
) -> float | None:
    if listing.price_czk is not None:
        return float(listing.price_czk)
    if listing.price_amount is None:
        return None
    currency = (listing.currency or "CZK").upper()
    if currency == "CZK":
        return float(listing.price_amount)
    if valuation is None:
        return None
    rate = valuation.exchange_rates_to_czk.get(currency)
    return float(listing.price_amount) * float(rate) if rate else None


def _score_components(
    *,
    net_profit: float | None,
    roi_percent: float | None,
    liquidity_score: int | None,
    confidence_score: int | None,
    condition: str,
    risk_flags: list[str],
    config: DealScoringConfig,
) -> tuple[float, dict[str, float]]:
    components: dict[str, float] = {}
    if net_profit is not None:
        components["profit"] = max(
            0.0,
            min(100.0, net_profit / config.profit_score_target_czk * 100),
        )
    if roi_percent is not None:
        components["roi"] = max(
            0.0,
            min(100.0, roi_percent / config.roi_score_target_percent * 100),
        )
    if liquidity_score is not None:
        components["liquidity"] = float(liquidity_score)
    if confidence_score is not None:
        components["confidence"] = float(confidence_score)
    if condition in config.condition_scores:
        components["condition"] = float(config.condition_scores[condition])
    components["risk"] = max(0.0, 100.0 - len(set(risk_flags)) * config.risk_penalty_per_flag)
    active_weights = {
        name: config.score_weights.get(name, 0.0)
        for name in components
        if config.score_weights.get(name, 0.0) > 0
    }
    total_weight = sum(active_weights.values())
    if total_weight <= 0:
        return 0.0, {name: round(value, 2) for name, value in components.items()}
    score = sum(components[name] * weight for name, weight in active_weights.items()) / total_weight
    return round(max(0.0, min(100.0, score)), 2), {
        name: round(value, 2) for name, value in components.items()
    }


def _config_snapshot(config: DealScoringConfig) -> dict[str, object]:
    return asdict(config)


class DealEvaluator:
    def __init__(self, config: DealScoringConfig) -> None:
        self.config = config

    def evaluate(
        self,
        listing: Listing,
        analysis: ListingAnalysis,
        costs: DealCosts | None = None,
    ) -> DealEvaluation:
        started = time.monotonic()
        costs = costs or DealCosts()
        LOGGER.info(
            "Deal evaluation started listing=%s algorithm=%s costs=%s",
            listing.key,
            self.config.algorithm_version,
            costs.to_dict(),
        )
        valuation = analysis.market_valuation
        condition, condition_flags = detect_condition(listing, self.config)
        liquidity_score, liquidity_level, estimated_days = calculate_liquidity(
            valuation,
            self.config,
        )
        confidence_level = valuation.confidence if valuation else "low"
        confidence_score = (
            self.config.confidence_scores.get(confidence_level)
            if valuation is not None
            else None
        )
        purchase_price = _purchase_price_czk(listing, valuation)
        market_median = float(valuation.market_price_czk) if valuation and valuation.market_price_czk is not None else None
        quick_sale_price = (
            float(valuation.quick_sale_price_czk)
            if valuation and valuation.quick_sale_price_czk is not None
            else None
        )
        missing_fields: list[str] = []
        flags = list(condition_flags)
        reasons: list[str] = []
        manual_reasons: list[str] = []
        invalid_costs = [
            name
            for name, value in costs.to_dict().items()
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0
        ]
        if invalid_costs:
            flags.append("invalid_additional_costs")
            manual_reasons.append("Дополнительные обязательные расходы содержат некорректное значение.")
            missing_fields.extend(invalid_costs)

        base_investment: float | None = None
        risk_reserve: float | None = None
        total_investment: float | None = None
        net_profit: float | None = None
        roi_percent: float | None = None
        if purchase_price is None or not math.isfinite(purchase_price):
            missing_fields.append("purchase_price_czk")
            manual_reasons.append("Не удалось нормализовать цену покупки в CZK.")
            flags.append("purchase_price_missing")
        elif not invalid_costs:
            purchase = Decimal(str(purchase_price))
            base = purchase + sum(
                Decimal(str(value))
                for value in (
                    costs.acquisition_costs_czk,
                    costs.logistics_costs_czk,
                    costs.platform_fees_czk,
                )
            )
            reserve = base * Decimal(str(self.config.risk_reserve_percent)) / Decimal("100")
            total = base + reserve
            base_investment = _money(base)
            risk_reserve = _money(reserve)
            total_investment = _money(total)
            if total <= 0:
                flags.append("non_positive_total_investment")
                manual_reasons.append("Полное вложение должно быть больше нуля.")
            elif quick_sale_price is not None and math.isfinite(quick_sale_price):
                profit = Decimal(str(quick_sale_price)) - total
                roi = profit / total * Decimal("100")
                net_profit = _money(profit)
                roi_percent = float(roi.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        if valuation is None:
            missing_fields.append("market_valuation")
            manual_reasons.append("Нет сохранённой оценки рынка этапа 2.1.")
            flags.append("market_valuation_missing")
        if quick_sale_price is None or not math.isfinite(quick_sale_price):
            missing_fields.append("quick_sale_price_czk")
            manual_reasons.append("Этап 2.1 не вернул цену быстрой продажи.")
            flags.append("quick_sale_price_missing")
        if analysis.identity is None or not analysis.identity.brand or not analysis.identity.model:
            manual_reasons.append("Модель велосипеда определена неоднозначно.")
            flags.append("ambiguous_identity")
        elif analysis.identity.confidence < self.config.minimum_identity_confidence:
            manual_reasons.append("Уверенность распознавания модели ниже допустимого порога.")
            flags.append("low_identity_confidence")
        if condition == "unknown":
            manual_reasons.append("Состояние велосипеда не подтверждено описанием.")
        elif condition == "service_required":
            manual_reasons.append("В описании указана необходимость обслуживания.")
        elif condition == "problematic":
            manual_reasons.append("В описании указана техническая проблема или ремонт.")
        elif condition == "contradictory":
            manual_reasons.append("Описание состояния содержит противоречивые признаки.")
        if self.config.manual_review_on_low_confidence and confidence_level == "low":
            manual_reasons.append("Оценка рынка этапа 2.1 имеет низкую уверенность.")
            flags.append("low_market_confidence")
        if valuation and valuation.status in self.config.critical_valuation_statuses:
            manual_reasons.append(f"Статус оценки рынка требует проверки: {valuation.status}.")
            flags.append("critical_valuation_status")
        if valuation:
            warning_text = " ".join(valuation.warnings).casefold()
            normalized_warnings = normalize_text(warning_text)
            if any(
                term.casefold() in warning_text
                or (
                    bool(normalize_text(term))
                    and normalize_text(term) in normalized_warnings
                )
                for term in self.config.critical_warning_terms
                if term
            ):
                manual_reasons.append("Этап 2.1 вернул критическое предупреждение.")
                flags.append("critical_market_warning")
        risk_text = " ".join(analysis.risks).casefold()
        normalized_risks = normalize_text(risk_text)
        if any(
            term.casefold() in risk_text
            or (
                bool(normalize_text(term))
                and normalize_text(term) in normalized_risks
            )
            for term in self.config.manual_review_risk_terms
            if term
        ):
            manual_reasons.append("Предварительный анализ содержит причину для ручной проверки.")
            flags.append("stage_1_2_manual_risk")
        if (
            purchase_price is not None
            and market_median is not None
            and market_median > 0
            and (market_median - purchase_price) / market_median * 100
            >= self.config.price_outlier_discount_percent
        ):
            manual_reasons.append("Цена покупки является подозрительно низким рыночным выбросом.")
            flags.append("purchase_price_outlier")

        hard_reject = (
            analysis.priority_class == "excluded"
            or analysis.notification_status == "excluded"
            or has_accessory_terms(listing.title)
        )
        if hard_reject:
            status = "REJECT"
            reasons.append("Объявление отклонено существующим фильтром релевантности.")
            flags.append("existing_hard_filter")
        elif net_profit is not None and net_profit <= 0:
            status = "REJECT"
            reasons.append(f"Чистая прибыль {net_profit:.0f} Kč не является положительной.")
            flags.append("non_positive_profit")
        elif roi_percent is not None and roi_percent <= 0:
            status = "REJECT"
            reasons.append(f"ROI {roi_percent:.2f}% не является положительным.")
            flags.append("non_positive_roi")
        elif manual_reasons:
            status = "MANUAL_REVIEW"
            reasons.extend(manual_reasons)
        else:
            assert net_profit is not None and roi_percent is not None
            scenario_a = (
                net_profit >= self.config.hot_min_profit_czk
                and roi_percent >= self.config.hot_min_roi_percent
            )
            scenario_b = net_profit > 0 and roi_percent >= self.config.high_roi_percent
            hot_financials = scenario_a or scenario_b
            hot_quality = (
                liquidity_score is not None
                and liquidity_score >= self.config.hot_min_liquidity_score
                and confidence_score is not None
                and confidence_score >= self.config.hot_min_confidence_score
                and liquidity_level in {"medium", "high"}
                and confidence_level in {"medium", "high"}
            )
            if hot_financials and hot_quality:
                status = "HOT"
                scenario = "высокой абсолютной прибыли" if scenario_a else "ROI не менее 100%"
                reasons.append(f"Выполнен сценарий HOT по правилу {scenario}.")
            elif hot_financials:
                status = "INTERESTING"
                reasons.append("Финансы проходят HOT, но ликвидности недостаточно для автоматического HOT.")
                flags.append("hot_blocked_by_liquidity")
            elif (
                net_profit >= self.config.hot_min_profit_czk
                and roi_percent >= self.config.interesting_min_roi_percent
            ) or (
                net_profit > 0
                and roi_percent >= self.config.interesting_min_roi_percent
                and liquidity_level in {"medium", "high"}
            ):
                status = "INTERESTING"
                reasons.append(
                    "Сделка имеет положительную прибыль и ROI не ниже пограничного порога, "
                    "но не проходит условия HOT."
                )
            else:
                status = "LOW_PRIORITY"
                reasons.append("Прибыль положительная, но финансовые показатели ниже порогов интересной сделки.")

        deal_score, components = _score_components(
            net_profit=net_profit,
            roi_percent=roi_percent,
            liquidity_score=liquidity_score,
            confidence_score=confidence_score,
            condition=condition,
            risk_flags=flags,
            config=self.config,
        )
        scope = market_scope(valuation)
        config_snapshot = _config_snapshot(self.config)
        market_input = None
        if valuation is not None:
            market_input = {
                "listing_source": valuation.listing_source,
                "listing_external_id": valuation.listing_external_id,
                "market_price_czk": valuation.market_price_czk,
                "quick_sale_price_czk": valuation.quick_sale_price_czk,
                "price_low_czk": valuation.price_low_czk,
                "price_high_czk": valuation.price_high_czk,
                "confidence": valuation.confidence,
                "valuation_method": valuation.valuation_method,
                "status": valuation.status,
                "comparables_total": valuation.comparables_total,
                "comparables_unique": valuation.comparables_unique,
                "exact_comparables": valuation.exact_comparables,
                "close_comparables": valuation.close_comparables,
                "family_comparables": valuation.family_comparables,
                "component_comparables": valuation.component_comparables,
                "cz_comparables": valuation.cz_comparables,
                "foreign_comparables": valuation.foreign_comparables,
                "countries_used": valuation.countries_used,
                "sources_used": valuation.sources_used,
                "warnings": valuation.warnings,
                "calculated_at": valuation.calculated_at,
            }
        fingerprint_payload = {
            "listing": listing.key,
            "listing_title": listing.title,
            "listing_description": listing.description,
            "purchase_price_czk": purchase_price,
            "costs": costs.to_dict(),
            "market_valuation": market_input,
            "identity": analysis.identity.to_dict() if analysis.identity else None,
            "condition": condition,
            "risks": analysis.risks,
            "priority_class": analysis.priority_class,
            "config": config_snapshot,
        }
        input_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        primary_reason = reasons[0] if reasons else "Решение рассчитано по правилам этапа 2.2."
        question = ""
        if status == "MANUAL_REVIEW":
            question = manual_reasons[0] if manual_reasons else "Проверьте исходные данные объявления."
        result = DealEvaluation(
            listing_source=listing.source,
            listing_external_id=listing.external_id,
            algorithm_version=self.config.algorithm_version,
            status=status,
            purchase_price_czk=purchase_price,
            acquisition_costs_czk=float(costs.acquisition_costs_czk),
            logistics_costs_czk=float(costs.logistics_costs_czk),
            platform_fees_czk=float(costs.platform_fees_czk),
            base_investment_czk=base_investment,
            risk_reserve_percent=self.config.risk_reserve_percent,
            risk_reserve_czk=risk_reserve,
            total_investment_czk=total_investment,
            market_median_czk=market_median,
            quick_sale_price_czk=quick_sale_price,
            net_profit_czk=net_profit,
            roi_percent=roi_percent,
            liquidity_score=liquidity_score,
            liquidity_level=liquidity_level,
            estimated_days_to_sell=estimated_days,
            confidence_score=confidence_score,
            confidence_level=confidence_level,
            condition=condition,
            market_scope=scope,
            comparable_count=valuation.comparables_unique if valuation else 0,
            source_count=len(set(valuation.sources_used)) if valuation else 0,
            valuation_status=valuation.status if valuation else "missing",
            market_valuation_ref=(
                f"{valuation.listing_source}:{valuation.listing_external_id}@{valuation.calculated_at}"
                if valuation
                else ""
            ),
            deal_score=deal_score,
            score_components=components,
            reasons=reasons,
            flags=sorted(set(flags)),
            missing_fields=sorted(set(missing_fields)),
            explanation=primary_reason,
            manual_review_question=question,
            config_snapshot=config_snapshot,
            input_fingerprint=input_fingerprint,
        )
        LOGGER.info(
            "Deal evaluation complete listing=%s algorithm=%s status=%s purchase=%s quick_sale=%s "
            "investment=%s profit=%s roi=%s score=%.2f flags=%s duration_ms=%d",
            listing.key,
            result.algorithm_version,
            result.status,
            result.purchase_price_czk,
            result.quick_sale_price_czk,
            result.total_investment_czk,
            result.net_profit_czk,
            result.roi_percent,
            result.deal_score,
            ",".join(result.flags),
            int((time.monotonic() - started) * 1000),
        )
        return result

    def evaluate_batch(
        self,
        items: list[tuple[Listing, ListingAnalysis, DealCosts | None]],
    ) -> list[DealEvaluation]:
        results: list[DealEvaluation] = []
        for listing, analysis, costs in items:
            try:
                results.append(self.evaluate(listing, analysis, costs))
            except Exception as exc:
                LOGGER.exception("Deal evaluation failed for %s; batch continues", listing.key)
                results.append(self.error_result(listing, exc))
        return results

    def error_result(
        self,
        listing: Listing,
        error: Exception,
    ) -> DealEvaluation:
        reason = f"Расчёт сделки завершился контролируемой ошибкой {type(error).__name__}."
        fingerprint = hashlib.sha256(
            f"{listing.key}|{self.config.algorithm_version}|{type(error).__name__}".encode("utf-8")
        ).hexdigest()
        return DealEvaluation(
            listing_source=listing.source,
            listing_external_id=listing.external_id,
            algorithm_version=self.config.algorithm_version,
            status="MANUAL_REVIEW",
            reasons=[reason],
            flags=["deal_evaluation_error"],
            missing_fields=["deal_evaluation"],
            explanation=reason,
            manual_review_question="Проверьте исходные данные и журнал ошибки расчёта сделки.",
            config_snapshot=_config_snapshot(self.config),
            input_fingerprint=fingerprint,
        )
