"""AI Analysis Level 2: оценка цены перепродажи.

Последняя ступень ценового каскада. Первые две — реальные аналоги с площадок
и амортизация от цены нового — требуют HTTP-запросов и потому упираются в
бюджет скрейпинга: из 63 объявлений цикла оценку получало одно. Эта ступень
запросов к площадкам не делает, поэтому закрывает остальные.

Важно понимать её природу: у модели нет живых данных чешского рынка, её ответ
— осведомлённое воспоминание, а не измерение. Отсюда три следствия в коде:
результат всегда диапазон, а не число; ответ проходит проверки вменяемости;
и статус HOT на такой оценке не выдаётся (см. ``price_allow_hot``).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from deal_radar.ai.client import AIResult, AIUnavailable, OpenAIClient
from deal_radar.ai.listing_analysis import content_fingerprint, sanitize_text
from deal_radar.ai.prompt_loader import PromptBundle, load_prompt
from deal_radar.config import AIConfig
from deal_radar.models import (
    AIAnalysis,
    AIPriceEstimate,
    BikeIdentity,
    Listing,
    MarketValuation,
    Valuation,
)

LOGGER = logging.getLogger(__name__)

CONFIDENCE_VALUES = ("high", "medium", "low")
BASIS_VALUES = ("SAME_MODEL", "SIMILAR_MODEL", "COMPONENT_CLASS", "GENERIC")
# Оценка на общих словах не заслуживает средней уверенности, чем бы модель
# себя ни оценила: она построена на категории, а не на конкретном велосипеде.
BASIS_CONFIDENCE_CAP = {"COMPONENT_CLASS": "medium", "GENERIC": "low"}
MAX_SUMMARY_CHARS = 240


def condition_bucket(analysis: AIAnalysis | None) -> str:
    """Состояние в грубых корзинах — цена внутри корзины меняется мало."""

    if analysis is None or analysis.condition is None:
        return "unknown"
    claimed = (analysis.condition.claimed_condition or "unknown").upper()
    if analysis.condition.service_needed or analysis.condition.defects:
        return f"{claimed}_ISSUES"
    return claimed


def estimate_key(
    listing: Listing,
    identity: BikeIdentity | None,
    ai_analysis: AIAnalysis | None,
) -> tuple[str, str]:
    """Ключ кэша и его тип.

    Когда бренд и модель известны, цена зависит от велосипеда, а не от
    объявления: «Merida Juliet 2018, хорошее состояние» стоит одинаково у
    любого продавца. Такой ключ разделяется между объявлениями и превращает
    сотни вызовов в десятки.

    Когда модель неизвестна, разделять кэш нельзя — иначе один ответ про
    «неизвестный велосипед» разошёлся бы по всем непонятным объявлениям сразу.
    Тогда ключ per-listing.
    """

    brand = (identity.brand if identity else "") or ""
    model = (identity.model if identity else "") or ""
    if not (brand and model):
        return content_fingerprint(listing), "listing"
    parts = (
        brand.casefold(),
        model.casefold(),
        str((identity.model_year if identity else None) or "unknown"),
        (identity.wheel_size if identity else "") or "unknown",
        "e" if (identity and identity.electric) else "n",
        condition_bucket(ai_analysis),
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest(), "identity"


def needs_estimate(valuation: MarketValuation | None, config: AIConfig) -> bool:
    """Нужна ли AI-оценка после детерминированного движка."""

    if valuation is None or valuation.market_price_czk is None:
        return True
    if valuation.comparables_unique < config.price_min_comparables:
        return True
    return bool(config.price_on_low_confidence and valuation.confidence == "low")


@dataclass(slots=True)
class PriceOutcome:
    estimate: AIPriceEstimate
    call_log: dict[str, Any] | None = None


def price_cost_usd(
    config: AIConfig,
    *,
    used_fallback: bool,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> float:
    if used_fallback:
        input_rate = config.fallback_input_usd_per_1m
        cached_rate = config.fallback_cached_input_usd_per_1m
        output_rate = config.fallback_output_usd_per_1m
    else:
        input_rate = config.price_input_usd_per_1m
        cached_rate = config.price_cached_input_usd_per_1m
        output_rate = config.price_output_usd_per_1m
    cached = max(0, min(cached_input_tokens, input_tokens))
    fresh = max(0, input_tokens - cached)
    total = (fresh * input_rate + cached * cached_rate + output_tokens * output_rate) / 1_000_000
    return round(total, 8)


class PriceEstimator:
    def __init__(
        self,
        config: AIConfig,
        client: OpenAIClient | None = None,
        prompt: PromptBundle | None = None,
    ) -> None:
        self.config = config
        self.client = client or OpenAIClient(config)
        self.prompt = prompt or load_prompt(
            config.price_prompt_name, config.price_prompt_version, config.prompts_path
        )

    # -- вход модели ---------------------------------------------------------

    def build_payload(
        self,
        listing: Listing,
        identity: BikeIdentity | None,
        ai_analysis: AIAnalysis | None,
        market: MarketValuation | None,
        retail: Valuation | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "listing": {
                "title": sanitize_text(listing.title, self.config.max_title_chars),
                "description": sanitize_text(listing.description, self.config.max_description_chars),
                "asking_price_czk": listing.price_czk,
                "currency": listing.currency,
                "location": sanitize_text(listing.location or "", 120) or None,
                "published_at": listing.published_at.isoformat() if listing.published_at else None,
            },
            "identity": self._identity_block(identity, ai_analysis),
            "partial_market_data": self._market_block(market, retail),
        }
        if ai_analysis is not None and ai_analysis.status == "AI_OK":
            if ai_analysis.specifications is not None:
                payload["specifications"] = asdict(ai_analysis.specifications)
            if ai_analysis.condition is not None:
                payload["condition"] = asdict(ai_analysis.condition)
        return payload

    def _identity_block(
        self, identity: BikeIdentity | None, ai_analysis: AIAnalysis | None
    ) -> dict[str, Any]:
        ai_identity = ai_analysis.identity if ai_analysis and ai_analysis.status == "AI_OK" else None
        return {
            "brand": (identity.brand if identity else "") or (ai_identity.brand if ai_identity else None),
            "model": (identity.model if identity else "") or (ai_identity.model if ai_identity else None),
            "model_year": (identity.model_year if identity else None)
            or (ai_identity.model_year if ai_identity else None),
            "bike_type": (identity.bike_type if identity else "")
            or (ai_identity.bike_type if ai_identity else None),
            "is_electric": identity.electric if identity else None,
            "wheel_size_inches": (identity.wheel_size if identity else "") or None,
            "frame_size": (identity.frame_size if identity else "") or None,
            "model_confirmed_by_catalog": bool(identity and identity.model_confirmed),
        }

    def _market_block(
        self, market: MarketValuation | None, retail: Valuation | None
    ) -> dict[str, Any]:
        """То, что успел собрать детерминированный движок.

        Даже пара найденных аналогов или цена нового велосипеда — куда лучший
        якорь, чем память модели, поэтому они передаются, если есть.
        """

        block: dict[str, Any] = {
            "engine_status": market.status if market else "not_attempted",
            "comparables_found": market.comparables_unique if market else 0,
            "new_bike_price_czk": retail.median_price_czk if retail else None,
            "comparables": [],
        }
        if market and market.comparables:
            block["comparables"] = [
                {
                    "price_czk": item.price_czk,
                    "country": item.country,
                    "title": sanitize_text(item.title or "", 120),
                }
                for item in market.comparables[:5]
                if item.price_czk
            ]
        return block

    # -- результаты без обращения к API --------------------------------------

    def _base(self, listing: Listing, identity, ai_analysis, status: str) -> AIPriceEstimate:
        key, _ = estimate_key(listing, identity, ai_analysis)
        return AIPriceEstimate(
            status=status,
            schema_version=self.prompt.schema_version,
            prompt_name=self.prompt.name,
            prompt_version=self.prompt.version,
            model_name=self.config.price_model,
            identity_key=key,
            condition_bucket=condition_bucket(ai_analysis),
        )

    def pending(self, listing, identity, ai_analysis, reason: str) -> AIPriceEstimate:
        estimate = self._base(listing, identity, ai_analysis, "PRICE_PENDING")
        estimate.reject_reason = reason
        return estimate

    # -- собственно оценка ---------------------------------------------------

    def estimate(
        self,
        listing: Listing,
        identity: BikeIdentity | None = None,
        ai_analysis: AIAnalysis | None = None,
        market: MarketValuation | None = None,
        retail: Valuation | None = None,
    ) -> PriceOutcome:
        """Одна оценка. Исключения наружу не выпускаются."""

        started_at = datetime.now(UTC)
        estimate = self._base(listing, identity, ai_analysis, "PRICE_FAILED")
        record: dict[str, Any] = {
            "request_id": uuid.uuid4().hex,
            "listing_source": listing.source,
            "listing_external_id": listing.external_id,
            "analysis_type": self.prompt.name,
            "model_name": self.config.price_model,
            "prompt_name": self.prompt.name,
            "prompt_version": self.prompt.version,
            "schema_version": self.prompt.schema_version,
            "started_at": started_at.isoformat(),
        }
        payload = self.build_payload(listing, identity, ai_analysis, market, retail)
        try:
            result = self.client.structured(
                system=self.prompt.system,
                user=self.prompt.build_user_message(payload),
                schema_name=self.prompt.schema_name,
                schema=self.prompt.schema,
                max_output_tokens=self.prompt.max_output_tokens,
                model_override=self.config.price_model,
            )
        except AIUnavailable as exc:
            estimate.error_type = type(exc).__name__
            estimate.error_message_safe = str(exc)[:500]
            record.update(
                finished_at=datetime.now(UTC).isoformat(),
                success=0,
                error_type=estimate.error_type,
                error_message_safe=estimate.error_message_safe,
            )
            return PriceOutcome(estimate=estimate, call_log=record)

        self._finish(estimate, result, listing)
        record.update(
            finished_at=datetime.now(UTC).isoformat(),
            model_name=result.model_name,
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            cached_input_tokens=result.cached_input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
            attempt_number=result.attempts,
            used_fallback=int(result.used_fallback),
            success=int(estimate.status == "PRICE_OK"),
            error_type=estimate.error_type,
            estimated_total_cost_usd=estimate.estimated_cost_usd,
            error_message_safe=estimate.error_message_safe,
        )
        return PriceOutcome(estimate=estimate, call_log=record)

    def _finish(self, estimate: AIPriceEstimate, result: AIResult, listing: Listing) -> None:
        estimate.model_name = result.model_name
        estimate.used_fallback = result.used_fallback
        estimate.input_tokens = result.input_tokens
        estimate.cached_input_tokens = result.cached_input_tokens
        estimate.output_tokens = result.output_tokens
        estimate.latency_ms = result.latency_ms
        estimate.estimated_cost_usd = price_cost_usd(
            self.config,
            used_fallback=result.used_fallback,
            input_tokens=result.input_tokens,
            cached_input_tokens=result.cached_input_tokens,
            output_tokens=result.output_tokens,
        )
        parse_price_payload(result.payload, estimate, listing, self.config)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(round(float(value)))
    return number if number > 0 else None


def parse_price_payload(
    payload: dict[str, Any],
    estimate: AIPriceEstimate,
    listing: Listing,
    config: AIConfig,
) -> AIPriceEstimate:
    """Разобрать ответ и проверить его на вменяемость.

    Structured Outputs гарантирует форму, но не смысл: модель может ошибиться
    порядком величины или перепутать кроны с евро. Проверки ниже отбрасывают
    такие ответы целиком — лучше пустая графа, чем неверная цена в расчёте
    выгоды.
    """

    market = _as_int(payload.get("market_price_czk"))
    low = _as_int(payload.get("price_low_czk"))
    high = _as_int(payload.get("price_high_czk"))
    if market is None:
        estimate.status = "PRICE_REJECTED"
        estimate.reject_reason = "no_price_returned"
        return estimate

    # Границы диапазона восстанавливаются, если модель их перепутала местами
    # или не дала вовсе: сам по себе порядок — не повод терять оценку.
    low = low or market
    high = high or market
    if low > high:
        low, high = high, low
    low = min(low, market)
    high = max(high, market)

    confidence = str(payload.get("confidence", "low")).casefold()
    if confidence not in CONFIDENCE_VALUES:
        confidence = "low"
    basis = str(payload.get("basis", "GENERIC")).upper()
    if basis not in BASIS_VALUES:
        basis = "GENERIC"
    capped = BASIS_CONFIDENCE_CAP.get(basis)
    if capped and CONFIDENCE_VALUES.index(confidence) < CONFIDENCE_VALUES.index(capped):
        confidence = capped

    warnings = [str(item)[:200] for item in payload.get("warnings", []) if str(item).strip()][:6]
    estimate.market_price_czk = market
    estimate.price_low_czk = low
    estimate.price_high_czk = high
    estimate.confidence = confidence
    estimate.basis = basis
    estimate.reasoning_summary = str(payload.get("reasoning_summary", ""))[:MAX_SUMMARY_CHARS]
    estimate.warnings = warnings

    reason = sanity_reject_reason(market, low, high, listing.price_czk, config)
    if reason:
        estimate.status = "PRICE_REJECTED"
        estimate.reject_reason = reason
        LOGGER.warning(
            "AI price estimate rejected for %s: %s (estimate %s CZK, asking %s CZK)",
            listing.key,
            reason,
            market,
            listing.price_czk,
        )
        return estimate
    estimate.status = "PRICE_OK"
    return estimate


def sanity_reject_reason(
    market: int,
    low: int,
    high: int,
    asking_price_czk: int | None,
    config: AIConfig,
) -> str:
    if not config.price_floor_czk <= market <= config.price_ceiling_czk:
        return "outside_absolute_bounds"
    if low and high and high / max(1, low) > config.price_max_range_ratio:
        return "range_too_wide"
    if asking_price_czk:
        ratio = market / asking_price_czk
        if ratio > config.price_max_ratio_to_asking:
            return "implausibly_above_asking"
        if ratio < config.price_min_ratio_to_asking:
            return "implausibly_below_asking"
    return ""


def to_market_valuation(
    estimate: AIPriceEstimate,
    listing: Listing,
    quick_sale_discount: float,
) -> MarketValuation | None:
    """Превратить принятую оценку в обычную ``MarketValuation``.

    Цена быстрой продажи считается тем же коэффициентом из конфига, что и для
    рыночных данных: финансовые пороги проекта менять нельзя, а собственный
    ответ модели про скидку стал бы вторым независимым порогом.
    """

    if estimate.status != "PRICE_OK" or estimate.market_price_czk is None:
        return None
    quick_sale = int(round(estimate.market_price_czk * (1 - quick_sale_discount)))
    return MarketValuation(
        listing_source=listing.source,
        listing_external_id=listing.external_id,
        market_price_czk=estimate.market_price_czk,
        quick_sale_price_czk=quick_sale,
        price_low_czk=estimate.price_low_czk,
        price_high_czk=estimate.price_high_czk,
        confidence=estimate.confidence,
        valuation_method="ai_estimate",
        status="ai_estimate",
        warnings=[*estimate.warnings, "Цена оценена AI: реальных аналогов на площадках не нашлось."],
        calculated_at=estimate.created_at,
    )
