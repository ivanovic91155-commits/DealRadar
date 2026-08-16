"""Проверка ответа модели на стороне приложения (раздел 6 ТЗ).

Structured Outputs со ``strict: true`` уже гарантирует форму, но полагаться
только на удалённую гарантию нельзя: ответ приходит из сети и попадает в базу.
Здесь тот же защитный разбор, что и в :mod:`deal_radar.codex_fallback` —
whitelist значений, клампы и обрезка, — только описанный схемой, чтобы списки
enum не разъезжались с ``schema.json``. Сторонний ``jsonschema`` не нужен.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from deal_radar.ai.client import AIInvalidResponse
from deal_radar.models import (
    AIClassification,
    AICondition,
    AIIdentity,
    AIOpportunity,
    AIRisk,
    AISpecifications,
)

MAX_TEXT = 200
MAX_EXCERPT = 120
MAX_LIST_ITEMS = 12


@dataclass(slots=True)
class ValidatedAnalysis:
    classification: AIClassification
    identity: AIIdentity
    specifications: AISpecifications
    condition: AICondition
    opportunity: AIOpportunity
    risk: AIRisk
    evidence: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _block(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _enum_values(schema: dict[str, Any], *path: str) -> set[str]:
    """Достать допустимые значения enum прямо из схемы промпта."""

    node: Any = schema
    for step in path:
        if not isinstance(node, dict):
            return set()
        if isinstance(node.get("items"), dict):
            node = node["items"]  # спуск внутрь массива, например evidence[]
        node = (node.get("properties") or {}).get(step)
    if not isinstance(node, dict):
        return set()
    values = node.get("enum")
    if values is None and isinstance(node.get("items"), dict):
        values = node["items"].get("enum")
    if not isinstance(values, list):
        return set()
    return {item for item in values if isinstance(item, str)}


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped[:limit] if stripped else None


def _enum(value: Any, allowed: set[str], default: str | None) -> str | None:
    text = _text(value)
    if text is None:
        return default
    if allowed and text not in allowed:
        return default
    return text


def _confidence(value: Any) -> float:
    try:
        return round(max(0.0, min(1.0, float(value))), 4)
    except (TypeError, ValueError):
        return 0.0


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_int(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    return number if low <= number <= high else None


def _optional_float(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return round(number, 2) if low <= number <= high else None


def _string_list(value: Any, allowed: set[str] | None = None) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for entry in value[:MAX_LIST_ITEMS]:
        text = _text(entry)
        if text is None or (allowed and text not in allowed):
            continue
        if text not in items:
            items.append(text)
    return items


def _evidence(value: Any, fields: set[str], sources: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    entries: list[dict[str, Any]] = []
    for entry in value[:MAX_LIST_ITEMS]:
        if not isinstance(entry, dict):
            continue
        name = _enum(entry.get("field"), fields, None)
        excerpt = _text(entry.get("excerpt"), MAX_EXCERPT)
        # Без цитаты из объявления запись ничего не доказывает.
        if name is None or excerpt is None:
            continue
        entries.append(
            {
                "field": name,
                "value": _text(entry.get("value")),
                "source": _enum(entry.get("source"), sources, "NONE"),
                "excerpt": excerpt,
            }
        )
    return entries


def parse_listing_analysis(payload: dict[str, Any], schema: dict[str, Any]) -> ValidatedAnalysis:
    """Привести ответ модели к типизированным блокам, отбросив всё лишнее."""

    if not isinstance(payload, dict) or not payload:
        raise AIInvalidResponse("analysis payload is empty")
    missing = [key for key in ("classification", "identity", "condition") if key not in payload]
    if missing:
        raise AIInvalidResponse(f"analysis payload is missing required blocks: {missing}")

    warnings = _string_list(payload.get("warnings"))

    classification_raw = _block(payload, "classification")
    classification = AIClassification(
        is_bicycle=bool(classification_raw.get("is_bicycle")),
        listing_type=_enum(
            classification_raw.get("listing_type"),
            _enum_values(schema, "classification", "listing_type"),
            "OTHER",
        )
        or "OTHER",
        relevance_confidence=_confidence(classification_raw.get("relevance_confidence")),
    )

    identity_raw = _block(payload, "identity")
    brand = _text(identity_raw.get("brand"), 60)
    model = _text(identity_raw.get("model"), 80)
    if model and not brand:
        # Модель без бренда не с чем сверять — держим её за недостоверную.
        warnings.append("ai_model_without_brand_discarded")
        model = None
    if model and brand and model.casefold() == brand.casefold():
        # Повтор бренда в поле модели — это и есть «модель по бренду».
        warnings.append("ai_model_repeated_brand_discarded")
        model = None
    identity = AIIdentity(
        brand=brand,
        model=model,
        generation=_text(identity_raw.get("generation"), 40),
        model_year=_optional_int(identity_raw.get("model_year"), 1970, 2100),
        bike_type=_enum(
            identity_raw.get("bike_type"), _enum_values(schema, "identity", "bike_type"), None
        ),
        is_electric=_optional_bool(identity_raw.get("is_electric")),
        identity_confidence=_confidence(identity_raw.get("identity_confidence")),
        manual_identification_needed=bool(
            identity_raw.get("manual_identification_needed", True)
        ),
    )
    if identity.model is None:
        identity.manual_identification_needed = True
    if not classification.is_bicycle and (identity.brand or identity.model):
        # У шлема POC Axion есть и бренд, и модель — только это не велосипед.
        # Если оставить их в разборе, карточка озаглавится «🚲 POC Axion», а
        # рыночная оценка пойдёт искать аналоги несуществующего велосипеда.
        # Тип и признак электро остаются: они помогают человеку при разборе.
        warnings.append("ai_identity_dropped_not_a_bicycle")
        identity.brand = None
        identity.model = None
        identity.generation = None
        identity.model_year = None
        identity.manual_identification_needed = True

    specifications_raw = _block(payload, "specifications")
    specifications = AISpecifications(
        frame_size_raw=_text(specifications_raw.get("frame_size_raw"), 40),
        frame_size_normalized=_enum(
            specifications_raw.get("frame_size_normalized"),
            _enum_values(schema, "specifications", "frame_size_normalized"),
            None,
        ),
        wheel_size_inches=_optional_float(specifications_raw.get("wheel_size_inches"), 10.0, 36.0),
        frame_material=_enum(
            specifications_raw.get("frame_material"),
            _enum_values(schema, "specifications", "frame_material"),
            None,
        ),
        fork=_text(specifications_raw.get("fork"), 80),
        groupset=_text(specifications_raw.get("groupset"), 80),
        brakes=_enum(
            specifications_raw.get("brakes"),
            _enum_values(schema, "specifications", "brakes"),
            None,
        ),
    )

    condition_raw = _block(payload, "condition")
    condition = AICondition(
        claimed_condition=_enum(
            condition_raw.get("claimed_condition"),
            _enum_values(schema, "condition", "claimed_condition"),
            "UNKNOWN",
        )
        or "UNKNOWN",
        service_needed=_optional_bool(condition_raw.get("service_needed")),
        defects=_string_list(condition_raw.get("defects")),
        missing_parts=_string_list(condition_raw.get("missing_parts")),
        condition_confidence=_confidence(condition_raw.get("condition_confidence")),
    )

    opportunity_raw = _block(payload, "opportunity")
    opportunity = AIOpportunity(
        seller_urgency=_enum(
            opportunity_raw.get("seller_urgency"),
            _enum_values(schema, "opportunity", "seller_urgency"),
            "UNKNOWN",
        )
        or "UNKNOWN",
        listing_quality=_enum(
            opportunity_raw.get("listing_quality"),
            _enum_values(schema, "opportunity", "listing_quality"),
            "MEDIUM",
        )
        or "MEDIUM",
        hidden_opportunity=bool(opportunity_raw.get("hidden_opportunity")),
    )

    risk_raw = _block(payload, "risk")
    risk = AIRisk(
        risk_flags=_string_list(
            risk_raw.get("risk_flags"), _enum_values(schema, "risk", "risk_flags")
        ),
        suspicious_price=bool(risk_raw.get("suspicious_price")),
        possible_stolen_bike=bool(risk_raw.get("possible_stolen_bike")),
        possible_scam=bool(risk_raw.get("possible_scam")),
    )

    return ValidatedAnalysis(
        classification=classification,
        identity=identity,
        specifications=specifications,
        condition=condition,
        opportunity=opportunity,
        risk=risk,
        evidence=_evidence(
            payload.get("evidence"),
            _enum_values(schema, "evidence", "field"),
            _enum_values(schema, "evidence", "source"),
        ),
        warnings=warnings[:MAX_LIST_ITEMS],
    )
