"""AI Analysis Level 1: объявление -> строгий JSON (разделы 5-7 ТЗ).

Класс не касается SQLite: кэш, журнал вызовов и бюджет живут в вызывающем коде
на главном потоке, а сам анализ выполняется в пуле потоков. Благодаря этому
соединение ``Storage`` остаётся однопоточным.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from deal_radar.ai.client import AIResult, AIUnavailable, OpenAIClient, estimate_cost_usd
from deal_radar.ai.prefilter import PrefilterResult
from deal_radar.ai.prompt_loader import PromptBundle, load_prompt
from deal_radar.ai.schema_validate import parse_listing_analysis
from deal_radar.bike_identity import identify_bike
from deal_radar.config import AIConfig
from deal_radar.models import AIAnalysis, BikeIdentity, Listing

LOGGER = logging.getLogger(__name__)

HTML_RE = re.compile(r"<[^>]+>")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s\-()]{7,}\d)(?!\d)")
WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")


def content_fingerprint(listing: Listing) -> str:
    """Отпечаток входных данных из раздела 3 ТЗ.

    Меняется только вместе с текстом, ценой или картинкой объявления, поэтому
    повторный анализ неизменившегося объявления не оплачивается ещё раз.
    """

    parts = (
        listing.source,
        listing.external_id,
        listing.title,
        listing.description,
        "" if listing.price_czk is None else str(listing.price_czk),
        listing.currency,
        listing.image_url or "",
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def sanitize_text(value: str, limit: int) -> str:
    """Убрать разметку и персональные данные продавца перед отправкой."""

    without_html = HTML_RE.sub(" ", value or "")
    without_email = EMAIL_RE.sub("[email]", without_html)
    without_phone = PHONE_RE.sub("[phone]", without_email)
    collapsed = WHITESPACE_RE.sub(" ", without_phone)
    return "\n".join(line.strip() for line in collapsed.splitlines() if line.strip())[:limit]


@dataclass(slots=True)
class AnalysisOutcome:
    analysis: AIAnalysis
    call_log: dict[str, Any] | None = None


class ListingAnalyzer:
    def __init__(
        self,
        config: AIConfig,
        client: OpenAIClient | None = None,
        prompt: PromptBundle | None = None,
    ) -> None:
        self.config = config
        self.client = client or OpenAIClient(config)
        self.prompt = prompt or load_prompt(
            config.prompt_name, config.prompt_version, config.prompts_path
        )

    # -- построение объявления для модели ------------------------------------

    def build_payload(self, listing: Listing, identity: BikeIdentity | None) -> dict[str, Any]:
        """Вход из раздела 5.1 ТЗ.

        Осознанное отличие: URL объявления в JSON не кладём — раздел 7 запрещает
        модели ссылаться на объявления и выдумывать URL. Первое фото при этом
        уходит отдельным каналом Responses API (``input_image``), а не как ссылка
        в тексте: модель получает саму картинку, а не задание её «открыть».
        В JSON остаётся только число фотографий — для риск-флага NO_PHOTOS.
        """

        hints: dict[str, Any] = {"possible_brand": None, "possible_model": None}
        if identity is not None:
            hints = {
                "possible_brand": identity.brand or None,
                "possible_model": identity.model or None,
                "model_confirmed_by_catalog": identity.model_confirmed,
            }
        return {
            "listing_id": listing.key,
            "source": listing.source,
            "source_listing_id": listing.external_id,
            "title": sanitize_text(listing.title, self.config.max_title_chars),
            "description": sanitize_text(listing.description, self.config.max_description_chars),
            "price": listing.price_czk,
            "currency": listing.currency,
            "location": sanitize_text(listing.location or "", 120) or None,
            "published_at": listing.published_at.isoformat() if listing.published_at else None,
            "image_count": 1 if listing.image_url else 0,
            "deterministic_hints": hints,
        }

    # -- результаты без обращения к API --------------------------------------

    def _base(self, listing: Listing, status: str) -> AIAnalysis:
        return AIAnalysis(
            status=status,
            schema_version=self.prompt.schema_version,
            prompt_name=self.prompt.name,
            prompt_version=self.prompt.version,
            model_name=self.config.model_primary,
            content_hash=content_fingerprint(listing),
        )

    def skipped(self, listing: Listing, prefilter: PrefilterResult) -> AIAnalysis:
        analysis = self._base(listing, "AI_SKIPPED")
        analysis.skip_reason_code = prefilter.reason_code
        analysis.warnings = [f"prefilter:{prefilter.matched_rule}"] if prefilter.matched_rule else []
        return analysis

    def pending(self, listing: Listing, reason_code: str) -> AIAnalysis:
        analysis = self._base(listing, "AI_PENDING")
        analysis.skip_reason_code = reason_code
        return analysis

    # -- собственно анализ ----------------------------------------------------

    def analyze(self, listing: Listing, identity: BikeIdentity | None = None) -> AnalysisOutcome:
        """Один вызов модели. Исключения наружу не выпускаются."""

        started_at = datetime.now(UTC)
        analysis = self._base(listing, "AI_FAILED")
        record: dict[str, Any] = {
            "request_id": uuid.uuid4().hex,
            "listing_source": listing.source,
            "listing_external_id": listing.external_id,
            "analysis_type": self.prompt.name,
            "model_name": self.config.model_primary,
            "prompt_name": self.prompt.name,
            "prompt_version": self.prompt.version,
            "schema_version": self.prompt.schema_version,
            "started_at": started_at.isoformat(),
        }
        user_message = self.prompt.build_user_message(self.build_payload(listing, identity))
        image_urls = (
            [listing.image_url]
            if self.config.vision_enabled and listing.image_url
            else None
        )
        vision_dropped = False
        try:
            result = self._structured(user_message, image_urls)
        except AIUnavailable as exc:
            # Фото могло не понравиться модели (например, она не мультимодальна).
            # Зрение не должно ухудшать текстовый разбор — повторяем без картинки.
            if image_urls is None:
                return self._failure(analysis, record, exc)
            try:
                result = self._structured(user_message, None)
            except AIUnavailable as exc2:
                return self._failure(analysis, record, exc2)
            vision_dropped = True

        analysis = self._finish(listing, analysis, result)
        if vision_dropped:
            analysis.warnings.append("ai_vision_unavailable_text_only")
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
            success=int(analysis.status == "AI_OK"),
            error_type=analysis.error_type,
            error_message_safe=analysis.error_message_safe,
            **self._costs(result),
        )
        return AnalysisOutcome(analysis=analysis, call_log=record)

    def _structured(self, user_message: str, image_urls: list[str] | None) -> AIResult:
        return self.client.structured(
            system=self.prompt.system,
            user=user_message,
            schema_name=self.prompt.schema_name,
            schema=self.prompt.schema,
            max_output_tokens=self.prompt.max_output_tokens,
            model_override=self.config.vision_model,
            image_urls=image_urls,
            image_detail=self.config.vision_detail,
        )

    def _failure(
        self, analysis: AIAnalysis, record: dict[str, Any], exc: AIUnavailable
    ) -> AnalysisOutcome:
        analysis.error_type = type(exc).__name__
        analysis.error_message_safe = str(exc)[:500]
        record.update(
            finished_at=datetime.now(UTC).isoformat(),
            success=0,
            error_type=analysis.error_type,
            error_message_safe=analysis.error_message_safe,
        )
        return AnalysisOutcome(analysis=analysis, call_log=record)

    def _costs(self, result: AIResult) -> dict[str, float]:
        total = estimate_cost_usd(
            self.config,
            used_fallback=result.used_fallback,
            input_tokens=result.input_tokens,
            cached_input_tokens=result.cached_input_tokens,
            output_tokens=result.output_tokens,
        )
        output_only = estimate_cost_usd(
            self.config,
            used_fallback=result.used_fallback,
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=result.output_tokens,
        )
        return {
            "estimated_input_cost_usd": round(total - output_only, 8),
            "estimated_output_cost_usd": output_only,
            "estimated_total_cost_usd": total,
        }

    def _finish(self, listing: Listing, analysis: AIAnalysis, result: AIResult) -> AIAnalysis:
        analysis.model_name = result.model_name
        analysis.used_fallback = result.used_fallback
        analysis.input_tokens = result.input_tokens
        analysis.cached_input_tokens = result.cached_input_tokens
        analysis.output_tokens = result.output_tokens
        analysis.latency_ms = result.latency_ms
        analysis.estimated_cost_usd = self._costs(result)["estimated_total_cost_usd"]
        try:
            validated = parse_listing_analysis(result.payload, self.prompt.schema)
        except AIUnavailable as exc:
            analysis.error_type = type(exc).__name__
            analysis.error_message_safe = str(exc)[:500]
            return analysis
        analysis.status = "AI_OK"
        analysis.classification = validated.classification
        analysis.identity = validated.identity
        analysis.specifications = validated.specifications
        analysis.condition = validated.condition
        analysis.opportunity = validated.opportunity
        analysis.risk = validated.risk
        analysis.evidence = {"items": validated.evidence}
        analysis.warnings = validated.warnings
        confirmed, source = confirm_against_catalog(
            validated.identity.brand, validated.identity.model
        )
        analysis.catalog_confirmed_model = confirmed
        analysis.catalog_model_source = source
        if validated.identity.model and not confirmed:
            analysis.warnings.append("ai_model_not_in_catalog")
        return analysis


def confirm_against_catalog(brand: str | None, model: str | None) -> tuple[str, str]:
    """Сверить предложение AI с каталогом этапа 1.

    Предложенная моделью строка сама по себе ничего не подтверждает — иначе
    вернулся бы ровно тот дефект, который чинил этап 1. Прогоняем «бренд +
    модель» через обычный распознаватель: подтверждением считается только
    попадание в ``bike_catalog.json``.
    """

    if not brand or not model:
        return "", ""
    identity = identify_bike(f"{brand} {model}")
    if identity.model_confirmed:
        return identity.model, identity.model_source
    return "", ""
