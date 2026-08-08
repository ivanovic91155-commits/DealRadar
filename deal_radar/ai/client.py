"""Клиент OpenAI Responses API поверх stdlib-хелперов из :mod:`deal_radar.http`.

Отдельный SDK намеренно не подключается: проекту нужен ровно один POST, а
``http.post_json`` уже умеет JSON-тело, заголовки и разбор ответа. Транспорт
инъецируется параметром ``poster`` — так же, как ``fetcher`` в остальных
модулях, поэтому тесты не ходят в сеть.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from deal_radar.config import AIConfig
from deal_radar.http import HttpError, post_json

LOGGER = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
AUTH_STATUS = {401, 403}


class AIUnavailable(RuntimeError):
    """AI-слой недоступен. Вызывающий код обязан деградировать, а не падать."""


class AIAuthError(AIUnavailable):
    """Ключ отсутствует, отозван или не даёт доступа к модели."""


class AIRequestError(AIUnavailable):
    """Запрос отвергнут как некорректный. Другая модель ответит так же."""


class AIInvalidResponse(AIUnavailable):
    """Ответ пришёл, но не является валидным результатом structured output."""


@dataclass(slots=True)
class AIResult:
    payload: dict[str, Any]
    model_name: str
    used_fallback: bool = False
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 1

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def estimate_cost_usd(
    config: AIConfig,
    *,
    used_fallback: bool,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> float:
    """Стоимость вызова по формуле раздела 16 ТЗ.

    Кэшированные входные токены тарифицируются отдельно и вычитаются из
    общего входного счётчика, который OpenAI возвращает уже с их учётом.
    """

    if used_fallback:
        input_rate = config.fallback_input_usd_per_1m
        cached_rate = config.fallback_cached_input_usd_per_1m
        output_rate = config.fallback_output_usd_per_1m
    else:
        input_rate = config.primary_input_usd_per_1m
        cached_rate = config.primary_cached_input_usd_per_1m
        output_rate = config.primary_output_usd_per_1m
    cached = max(0, min(cached_input_tokens, input_tokens))
    uncached = max(0, input_tokens - cached)
    total = (
        uncached / 1_000_000 * input_rate
        + cached / 1_000_000 * cached_rate
        + max(0, output_tokens) / 1_000_000 * output_rate
    )
    return round(total, 8)


def _usage(response: dict[str, Any]) -> tuple[int, int, int]:
    usage = response.get("usage") or {}
    details = usage.get("input_tokens_details") or {}

    def number(source: dict[str, Any], key: str) -> int:
        try:
            return max(0, int(source.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    return (
        number(usage, "input_tokens"),
        number(details, "cached_tokens"),
        number(usage, "output_tokens"),
    )


def _extract_payload(response: dict[str, Any]) -> dict[str, Any]:
    """Достать JSON структурированного ответа из конверта Responses API."""

    status = str(response.get("status", ""))
    if status == "incomplete":
        reason = (response.get("incomplete_details") or {}).get("reason", "unknown")
        raise AIInvalidResponse(f"response incomplete: {reason}")
    for item in response.get("output") or []:
        if item.get("type") != "message":
            continue
        for chunk in item.get("content") or []:
            chunk_type = chunk.get("type")
            if chunk_type == "refusal":
                raise AIInvalidResponse(f"model refused: {str(chunk.get('refusal', ''))[:200]}")
            if chunk_type == "output_text":
                text = chunk.get("text") or ""
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise AIInvalidResponse(f"model returned non-JSON output: {exc}") from exc
                if not isinstance(parsed, dict):
                    raise AIInvalidResponse("model returned JSON that is not an object")
                return parsed
    raise AIInvalidResponse("response contained no output_text block")


class OpenAIClient:
    def __init__(
        self,
        config: AIConfig,
        poster: Callable[..., dict[str, Any]] = post_json,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._poster = poster
        self._sleeper = sleeper
        self._clock = clock

    @property
    def url(self) -> str:
        return f"{self.config.api_base_url.rstrip('/')}/responses"

    def _redact(self, text: str) -> str:
        """Никакая часть ключа не должна попасть в лог или в базу."""

        key = self.config.api_key
        return text.replace(key, "***") if key else text

    def structured(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        max_output_tokens: int = 2000,
    ) -> AIResult:
        """Один structured-output запрос с ретраями и переходом на fallback-модель."""

        if not self.config.api_key:
            raise AIAuthError("OPENAI_API_KEY is not configured")
        models: list[tuple[str, bool]] = [(self.config.model_primary, False)]
        if self.config.fallback_enabled and self.config.model_fallback != self.config.model_primary:
            models.append((self.config.model_fallback, True))

        last_error: AIUnavailable | None = None
        for model_name, used_fallback in models:
            try:
                return self._call_with_retries(
                    model_name=model_name,
                    used_fallback=used_fallback,
                    system=system,
                    user=user,
                    schema_name=schema_name,
                    schema=schema,
                    max_output_tokens=max_output_tokens,
                    # Ретраи уже потрачены на основную модель; fallback получает
                    # одну попытку, иначе один зависший вызов съедает весь цикл.
                    attempts=1 if used_fallback else self.config.max_retries + 1,
                )
            except (AIAuthError, AIRequestError):
                # Проблема в ключе или в самом запросе — другая модель не поможет.
                raise
            except AIUnavailable as exc:
                last_error = exc
                if used_fallback:
                    break
                LOGGER.warning(
                    "AI model %s failed (%s); trying fallback model", model_name, type(exc).__name__
                )
        raise last_error or AIUnavailable("AI request failed")

    def _call_with_retries(
        self,
        *,
        model_name: str,
        used_fallback: bool,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        max_output_tokens: int,
        attempts: int,
    ) -> AIResult:
        payload = {
            "model": model_name,
            "input": [
                {"role": "developer", "content": system},
                {"role": "user", "content": user},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                }
            },
            "max_output_tokens": max_output_tokens,
            # Содержимое объявлений не должно оставаться на стороне OpenAI.
            "store": False,
        }
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        last_error: AIUnavailable | None = None
        for attempt in range(1, attempts + 1):
            started = self._clock()
            try:
                response = self._poster(
                    self.url, payload, headers, timeout=self.config.timeout_seconds
                )
                result = _extract_payload(response)
            except HttpError as exc:
                last_error = self._translate(exc)
                if not self._retryable(exc):
                    raise last_error
            except AIInvalidResponse as exc:
                last_error = exc
            else:
                input_tokens, cached_tokens, output_tokens = _usage(response)
                return AIResult(
                    payload=result,
                    model_name=model_name,
                    used_fallback=used_fallback,
                    input_tokens=input_tokens,
                    cached_input_tokens=cached_tokens,
                    output_tokens=output_tokens,
                    latency_ms=int((self._clock() - started) * 1000),
                    attempts=attempt,
                )
            if attempt < attempts:
                self._sleeper(2.0 ** (attempt - 1))
        raise last_error or AIUnavailable(f"AI request to {model_name} failed")

    @staticmethod
    def _retryable(exc: HttpError) -> bool:
        if exc.status_code is None:
            # Таймаут или сетевая ошибка без HTTP-кода — повторяем.
            return True
        return exc.status_code in RETRYABLE_STATUS

    def _translate(self, exc: HttpError) -> AIUnavailable:
        message = self._redact(str(exc))[:500]
        status = exc.status_code
        if status in AUTH_STATUS:
            return AIAuthError(message)
        # 404 значит «нет такой модели» — это как раз повод попробовать вторую.
        if status is not None and 400 <= status < 500 and status not in RETRYABLE_STATUS | {404}:
            return AIRequestError(message)
        return AIUnavailable(message)
