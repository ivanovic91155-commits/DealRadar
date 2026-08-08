"""Каталог брендов и моделей велосипедов.

Модель считается подтверждённой только тогда, когда её удалось найти в этом
каталоге. Свободные слова после названия бренда подтверждённой моделью стать
не могут — см. `deal_radar/bike_identity.py`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from deal_radar.text_utils import normalize_text

LOGGER = logging.getLogger(__name__)

DEFAULT_CATALOG_PATH = Path(__file__).with_name("data") / "bike_catalog.json"

# Номер комплектации сразу после названия модели: "Marlin 7", "Scale 970", "Aim 29er".
VARIANT_RE = re.compile(r"\d{1,4}(?:\.\d)?[a-z]{0,2}")
MEASUREMENT_UNITS = frozenset({"cm", "mm", "kg", "km", "palcu", "palce", "palec", "inch"})
NUMERIC_ALIAS_RE = re.compile(r"\d+(?:\.\d+)?")
MIN_FUZZY_ALIAS_LENGTH = 4


@dataclass(slots=True, frozen=True)
class CatalogEntry:
    canonical: str
    electric: bool | None = None
    audience: str = ""


@dataclass(slots=True, frozen=True)
class CatalogMatch:
    model: str
    canonical: str
    source: str
    electric: bool | None = None
    audience: str = ""


@dataclass(slots=True)
class Catalog:
    # Бренд -> алиасы (нормализованные), отсортированные от самых длинных.
    brands: dict[str, tuple[tuple[str, CatalogEntry], ...]] = field(default_factory=dict)
    motor_keywords: tuple[str, ...] = ()

    def aliases_for(self, brand_key: str) -> tuple[tuple[str, CatalogEntry], ...]:
        return self.brands.get(brand_key, ())

    def __bool__(self) -> bool:
        return bool(self.brands)


_CACHE: dict[str, Catalog] = {}
_PATTERNS: dict[str, re.Pattern[str]] = {}


def _word_pattern(alias: str) -> re.Pattern[str]:
    pattern = _PATTERNS.get(alias)
    if pattern is None:
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])")
        _PATTERNS[alias] = pattern
    return pattern


def _build_catalog(raw: dict) -> Catalog:
    brands: dict[str, tuple[tuple[str, CatalogEntry], ...]] = {}
    for brand_key, brand_raw in (raw.get("brands") or {}).items():
        brand_audience = str(brand_raw.get("audience", ""))
        indexed: list[tuple[str, CatalogEntry]] = []
        for model_raw in brand_raw.get("models") or []:
            canonical = str(model_raw.get("name", "")).strip()
            if not canonical:
                continue
            electric = model_raw.get("electric")
            entry = CatalogEntry(
                canonical=canonical,
                electric=bool(electric) if electric is not None else None,
                audience=str(model_raw.get("audience", "")) or brand_audience,
            )
            names = [canonical, *(str(item) for item in model_raw.get("aliases") or [])]
            for name in names:
                alias = normalize_text(name)
                if alias:
                    indexed.append((alias, entry))
        # Более длинные алиасы проверяются первыми: "fuel ex" должен выигрывать у "fx",
        # "big nine" — у "big".
        indexed.sort(key=lambda item: (len(item[0].split()), len(item[0])), reverse=True)
        brands[normalize_text(brand_key)] = tuple(indexed)
    motors = tuple(
        normalize_text(str(item)) for item in raw.get("motor_keywords") or [] if str(item).strip()
    )
    return Catalog(brands=brands, motor_keywords=motors)


def load_catalog(path: str | Path = "") -> Catalog:
    """Загрузить каталог с кэшированием по пути.

    Отсутствующий или повреждённый файл не является фатальной ошибкой: система
    деградирует к неподтверждённым моделям-кандидатам.
    """
    resolved = Path(path) if path else DEFAULT_CATALOG_PATH
    cache_key = str(resolved)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
        catalog = _build_catalog(raw)
    except FileNotFoundError:
        LOGGER.warning("Bike catalog not found: %s — models stay unconfirmed", resolved)
        catalog = Catalog()
    except (OSError, ValueError) as error:
        LOGGER.warning("Bike catalog is unreadable (%s): %s", resolved, error)
        catalog = Catalog()
    _CACHE[cache_key] = catalog
    return catalog


def clear_cache() -> None:
    _CACHE.clear()


def _variant_after(text: str, end: int, stop_tokens: frozenset[str]) -> str:
    tail = text[end:].split()
    if not tail:
        return ""
    token = tail[0].strip(".-")
    if not token or token in stop_tokens:
        return ""
    if not VARIANT_RE.fullmatch(token):
        return ""
    # "Sauron vel. S (150-165 cm)" — рост ездока, а не вариант модели. Число,
    # за которым идёт ещё одно число или единица измерения, вариантом не бывает:
    # настоящие варианты ("Marlin 7", "Juliet 7.100") стоят особняком.
    following = tail[1].strip(".-") if len(tail) > 1 else ""
    if following in MEASUREMENT_UNITS or VARIANT_RE.fullmatch(following or "x"):
        return ""
    return token


def _exact_match(
    alias: str,
    entry: CatalogEntry,
    text: str,
    stop_tokens: frozenset[str],
    allow_variant: bool,
) -> CatalogMatch | None:
    found = _word_pattern(alias).search(text)
    if not found:
        return None
    variant = _variant_after(text, found.end(), stop_tokens) if allow_variant else ""
    model = f"{entry.canonical} {variant}".strip()
    return CatalogMatch(
        model=model,
        canonical=entry.canonical,
        source="catalog",
        electric=entry.electric,
        audience=entry.audience,
    )


def _fuzzy_match(
    aliases: tuple[tuple[str, CatalogEntry], ...],
    tail: str,
    stop_tokens: frozenset[str],
    cutoff: float,
) -> CatalogMatch | None:
    tokens = [token for token in tail.split() if token]
    if not tokens or cutoff >= 1:
        return None
    best_ratio = cutoff
    best: tuple[CatalogEntry, int] | None = None
    for alias, entry in aliases:
        if len(alias) < MIN_FUZZY_ALIAS_LENGTH or NUMERIC_ALIAS_RE.fullmatch(alias):
            continue
        width = len(alias.split())
        for index in range(len(tokens) - width + 1):
            window = " ".join(tokens[index : index + width])
            if window in stop_tokens:
                continue
            ratio = SequenceMatcher(None, alias, window).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = (entry, index + width)
    if best is None:
        return None
    entry, next_index = best
    variant = tokens[next_index].strip(".-") if next_index < len(tokens) else ""
    if variant in stop_tokens or not VARIANT_RE.fullmatch(variant or "x"):
        variant = ""
    return CatalogMatch(
        model=f"{entry.canonical} {variant}".strip(),
        canonical=entry.canonical,
        source="catalog_fuzzy",
        electric=entry.electric,
        audience=entry.audience,
    )


def match_model(
    brand_key: str,
    tail: str,
    normalized_title: str,
    normalized_all: str,
    *,
    catalog: Catalog,
    fuzzy_cutoff: float = 0.9,
    stop_tokens: frozenset[str] = frozenset(),
) -> CatalogMatch | None:
    """Найти модель бренда в тексте объявления.

    `tail` — часть заголовка после названия бренда, уже очищенная от года,
    поколения и размеров. Чисто числовые названия моделей (Woom 1–6) ищутся
    только в нём, чтобы случайные числа не превращались в модель.
    """
    aliases = catalog.aliases_for(normalize_text(brand_key))
    if not aliases:
        return None
    texts = [text for text in (tail, normalized_title, normalized_all) if text]
    for position, text in enumerate(texts):
        for alias, entry in aliases:
            numeric_alias = bool(NUMERIC_ALIAS_RE.fullmatch(alias))
            if numeric_alias and position > 0:
                continue
            match = _exact_match(alias, entry, text, stop_tokens, allow_variant=not numeric_alias)
            if match:
                return match
    return _fuzzy_match(aliases, tail, stop_tokens, fuzzy_cutoff)


def motor_keywords(catalog: Catalog) -> tuple[str, ...]:
    return catalog.motor_keywords
