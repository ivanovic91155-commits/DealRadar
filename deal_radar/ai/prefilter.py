"""Дешёвый фильтр перед платным вызовом AI (раздел 4 ТЗ).

Модуль отвечает ровно на один вопрос: тратить ли на это объявление вызов API.
Он не меняет статусы и не подменяет :func:`deal_radar.bike_identity.hard_filter_reason`,
а только переиспользует его — тот вызывается из ``priority`` и ``deal_scoring``,
и любая правка там сдвинула бы сегодняшние решения по сделкам.

Отдельно важно, чего фильтр НЕ делает: он не отсекает объявления за краткость,
за отсутствие модели и (по умолчанию) за отсутствие цены. «Prodám Scott, málo
jeté, spěchá» обязано дойти до AI — именно такие объявления и есть скрытая
возможность.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from deal_radar.bike_identity import hard_filter_reason
from deal_radar.config import AIConfig
from deal_radar.models import BikeIdentity, Listing
from deal_radar.text_utils import normalize_text

# Ключевые слова «куплю» на языках площадок. Ищем только в заголовке: в
# описании «koupím si nové» встречается у обычных продаж.
WANTED_TERMS: tuple[str, ...] = (
    "koupim",
    "kupim",
    "shanim",
    "poptavka",
    "poptavam",
    "hledam",
    "wanted",
    "looking for",
    "suche",
    "gesucht",
    "kaufe",
    "zoek",
    "gezocht",
    "kupie",
    "szukam",
)
WANTED_RE = re.compile(r"\b(?:%s)\b" % "|".join(term.replace(" ", r"\s+") for term in WANTED_TERMS))

REASON_PARTS = "PARTS_OR_ACCESSORY"
REASON_KIDS = "KIDS_BIKE"
REASON_WANTED = "WANTED_AD"
REASON_NO_PRICE = "NO_PRICE"
REASON_PRICE_RANGE = "PRICE_OUT_OF_RANGE"
REASON_DUPLICATE = "DUPLICATE"
REASON_UNCHANGED = "UNCHANGED"
REASON_BUDGET = "BUDGET_EXHAUSTED"

_HARD_FILTER_REASONS = {
    "hard_filter_accessory_or_part": REASON_PARTS,
    "hard_filter_kids_bike": REASON_KIDS,
}


@dataclass(slots=True, frozen=True)
class PrefilterResult:
    passed: bool
    reason_code: str = ""
    matched_rule: str = ""
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Форма записи в лог из раздела 4 ТЗ."""

        return {
            "passed": self.passed,
            "reason_code": self.reason_code,
            "matched_rule": self.matched_rule,
            "checked_at": self.checked_at,
        }


def _rejected(reason_code: str, matched_rule: str) -> PrefilterResult:
    return PrefilterResult(passed=False, reason_code=reason_code, matched_rule=matched_rule)


def looks_like_wanted_ad(title: str) -> tuple[bool, str]:
    match = WANTED_RE.search(normalize_text(title))
    return (True, f"wanted_keyword:{match.group(0)}") if match else (False, "")


def ai_prefilter(
    listing: Listing,
    config: AIConfig,
    *,
    identity: BikeIdentity | None = None,
    is_duplicate: bool = False,
    min_price_czk: int | None = None,
    max_price_czk: int | None = None,
) -> PrefilterResult:
    """Решить, отправлять ли объявление в AI, и назвать причину отказа."""

    if is_duplicate:
        return _rejected(REASON_DUPLICATE, "confirmed_cross_source_duplicate")

    hard_reason = hard_filter_reason(listing, identity)
    if hard_reason:
        return _rejected(_HARD_FILTER_REASONS.get(hard_reason, REASON_PARTS), hard_reason)

    wanted, rule = looks_like_wanted_ad(listing.title)
    if wanted:
        return _rejected(REASON_WANTED, rule)

    if listing.price_czk is None:
        if config.skip_listings_without_price:
            return _rejected(REASON_NO_PRICE, "price_missing")
        # Договорная цена — не повод пропускать анализ; решает уже deal engine.
        return PrefilterResult(passed=True)

    if min_price_czk is not None and listing.price_czk < min_price_czk:
        return _rejected(REASON_PRICE_RANGE, f"below_min:{min_price_czk}")
    if max_price_czk is not None and listing.price_czk > max_price_czk:
        return _rejected(REASON_PRICE_RANGE, f"above_max:{max_price_czk}")
    return PrefilterResult(passed=True)
