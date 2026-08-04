"""Конфигурация скоринга сделок.

Вся формула DealScore управляется этим модулем: веса компонентов, точки
перевода маржи в баллы, кривая амортизации, списки ликвидных брендов и
красные флаги. В коде движка (deal_engine.py) не должно быть ни одного
магического числа — только обращения к этому конфигу. Так пороги и веса
можно калибровать без изменения логики.

Значения по умолчанию — стартовые. Их предполагается подкручивать по мере
накопления реальной статистики перепродаж.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# --- Веса компонентов DealScore (в сумме должны давать 1.0) ------------------

@dataclass(slots=True)
class ScoreWeights:
    margin: float = 0.40
    liquidity: float = 0.22
    risk: float = 0.18
    quality: float = 0.10
    freshness: float = 0.10

    def total(self) -> float:
        return self.margin + self.liquidity + self.risk + self.quality + self.freshness


# --- Перевод прибыли (Kč) в баллы маржи (0-100) ------------------------------
# Список пар (прибыль_Kč, баллы). Между точками — линейная интерполяция.
# Ниже первой точки — 0 баллов, выше последней — балл последней точки.
DEFAULT_MARGIN_POINTS: list[tuple[int, float]] = [
    (0, 0.0),
    (2000, 60.0),
    (4000, 80.0),
    (8000, 95.0),
    (12000, 100.0),
]


# --- Кривая амортизации: возраст (лет) -> доля от цены нового -----------------
# Ключ — возраст в годах; "unknown" — когда год не распознан (консервативно).
DEFAULT_AGE_FACTORS: dict[str, float] = {
    "0": 0.82,
    "1": 0.72,
    "2": 0.63,
    "3": 0.55,
    "4": 0.47,
    "5+": 0.38,
    "unknown": 0.55,
}
DEFAULT_AGE_FACTORS_EBIKE: dict[str, float] = {
    "0": 0.74,
    "1": 0.62,
    "2": 0.52,
    "3": 0.43,
    "4": 0.35,
    "5+": 0.28,
    "unknown": 0.46,
}


# --- Ликвидность -------------------------------------------------------------
# Бренды с быстрым оборотом на вторичке в Праге. Баллы 0-100.
DEFAULT_BRAND_LIQUIDITY: dict[str, float] = {
    "trek": 90.0,
    "specialized": 90.0,
    "cube": 88.0,
    "giant": 85.0,
    "scott": 85.0,
    "kellys": 80.0,
    "merida": 80.0,
    "canyon": 82.0,
    "ktm": 75.0,
    "author": 72.0,
    "rock machine": 72.0,
    "ghost": 72.0,
    "focus": 70.0,
    "orbea": 70.0,
    "kross": 68.0,
    "superior": 68.0,
}
DEFAULT_BRAND_LIQUIDITY_FALLBACK: float = 45.0  # неизвестный/нишевый бренд

# Размер рамы -> модификатор ликвидности (добавляется к брендовому баллу).
DEFAULT_FRAME_SIZE_MODIFIER: dict[str, float] = {
    "M": 10.0,
    "L": 10.0,
    "M/L": 10.0,
    "S": 3.0,
    "XL": -5.0,
    "XS": -10.0,
    "XXS": -15.0,
    "XXL": -12.0,
}

# Тип велосипеда -> модификатор ликвидности.
DEFAULT_TYPE_MODIFIER: dict[str, float] = {
    "mountain": 8.0,
    "gravel": 8.0,
    "city": 5.0,
    "road": 3.0,
    "bmx": -10.0,
}


# --- Красные флаги риска -----------------------------------------------------
# Риск стартует со 100 и уменьшается за каждый сработавший флаг.
@dataclass(slots=True)
class RiskPenalties:
    # Цена подозрительно низкая (< price_ratio_floor от ожидаемой рыночной).
    suspicious_low_price: float = 40.0
    price_ratio_floor: float = 0.35
    no_photo: float = 25.0
    short_description: float = 15.0
    short_description_word_threshold: int = 15
    unrecognized_price: float = 20.0
    # Слова в тексте, каждая группа — свой штраф.
    parts_or_no_docs: float = 50.0   # na díly, bez dokladů, kradené
    damaged: float = 20.0            # poškozené, nefunkční


# Стоп-слова риска (нормализованные, без диакритики). Проверяются в тексте.
DEFAULT_PARTS_NODOCS_WORDS: tuple[str, ...] = (
    "na dily", "nadily", "bez dokladu", "bez faktury", "kradene", "kradeny",
    "pouze dily", "jen dily",
)
DEFAULT_DAMAGED_WORDS: tuple[str, ...] = (
    "poskozene", "poskozeny", "nefunkcni", "rozbite", "rozbity", "havarovane",
    "prasklina", "prasklý ram", "praskly ram",
)


# --- Качество объявления -----------------------------------------------------
@dataclass(slots=True)
class QualityBonuses:
    has_photo: float = 40.0
    description_per_word: float = 1.5      # до потолка
    description_word_cap: int = 30
    has_frame_size: float = 15.0
    has_year: float = 15.0
    # База, чтобы даже пустое объявление не давало жёсткий ноль.
    base: float = 10.0


# --- Свежесть ----------------------------------------------------------------
@dataclass(slots=True)
class FreshnessCurve:
    full_score_minutes: int = 15     # <= этого возраста — 100 баллов
    zero_score_minutes: int = 180    # >= этого — 0 баллов
    # Между этими точками — линейный спад.


# --- Маршрутизация -----------------------------------------------------------
@dataclass(slots=True)
class RoutingThresholds:
    hot_min_score: float = 70.0
    hot_max_age_minutes: int = 15
    notify_min_score: float = 40.0   # ниже — только в базу


# --- Верхнеуровневый конфиг --------------------------------------------------
@dataclass(slots=True)
class ScoringConfig:
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    margin_points: list[tuple[int, float]] = field(
        default_factory=lambda: list(DEFAULT_MARGIN_POINTS)
    )
    age_factors: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_AGE_FACTORS)
    )
    age_factors_ebike: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_AGE_FACTORS_EBIKE)
    )
    brand_liquidity: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_BRAND_LIQUIDITY)
    )
    brand_liquidity_fallback: float = DEFAULT_BRAND_LIQUIDITY_FALLBACK
    frame_size_modifier: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_FRAME_SIZE_MODIFIER)
    )
    type_modifier: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_TYPE_MODIFIER)
    )
    risk: RiskPenalties = field(default_factory=RiskPenalties)
    parts_nodocs_words: tuple[str, ...] = DEFAULT_PARTS_NODOCS_WORDS
    damaged_words: tuple[str, ...] = DEFAULT_DAMAGED_WORDS
    quality: QualityBonuses = field(default_factory=QualityBonuses)
    freshness: FreshnessCurve = field(default_factory=FreshnessCurve)
    routing: RoutingThresholds = field(default_factory=RoutingThresholds)

    # Издержки одной перепродажи (Kč): чистка, время, риск, комиссии.
    fixed_costs_czk: int = 1500

    def validate(self) -> None:
        total = self.weights.total()
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"Веса компонентов должны давать 1.0, сейчас {total:.3f}")
        if len(self.margin_points) < 2:
            raise ValueError("margin_points должен содержать минимум 2 точки")
        prev = None
        for profit, score in self.margin_points:
            if prev is not None and profit <= prev:
                raise ValueError("margin_points должны идти по возрастанию прибыли")
            prev = profit
        if not 0 < self.routing.notify_min_score <= self.routing.hot_min_score <= 100:
            raise ValueError("Пороги маршрутизации: 0 < notify <= hot <= 100")
        if self.freshness.full_score_minutes >= self.freshness.zero_score_minutes:
            raise ValueError("freshness: full_score_minutes должен быть меньше zero_score_minutes")

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ScoringConfig":
        """Строит конфиг из JSON-словаря, накладывая переданные значения
        поверх дефолтов. Отсутствующие ключи берутся из значений по умолчанию.
        """
        config = cls()
        if not raw:
            config.validate()
            return config

        if "weights" in raw:
            w = raw["weights"]
            config.weights = ScoreWeights(
                margin=float(w.get("margin", config.weights.margin)),
                liquidity=float(w.get("liquidity", config.weights.liquidity)),
                risk=float(w.get("risk", config.weights.risk)),
                quality=float(w.get("quality", config.weights.quality)),
                freshness=float(w.get("freshness", config.weights.freshness)),
            )
        if "margin_points" in raw:
            config.margin_points = [(int(p), float(s)) for p, s in raw["margin_points"]]
        if "age_factors" in raw:
            config.age_factors = {str(k): float(v) for k, v in raw["age_factors"].items()}
        if "age_factors_ebike" in raw:
            config.age_factors_ebike = {str(k): float(v) for k, v in raw["age_factors_ebike"].items()}
        if "brand_liquidity" in raw:
            config.brand_liquidity = {str(k).casefold(): float(v) for k, v in raw["brand_liquidity"].items()}
        if "brand_liquidity_fallback" in raw:
            config.brand_liquidity_fallback = float(raw["brand_liquidity_fallback"])
        if "frame_size_modifier" in raw:
            config.frame_size_modifier = {str(k).upper(): float(v) for k, v in raw["frame_size_modifier"].items()}
        if "type_modifier" in raw:
            config.type_modifier = {str(k): float(v) for k, v in raw["type_modifier"].items()}
        if "fixed_costs_czk" in raw:
            config.fixed_costs_czk = int(raw["fixed_costs_czk"])
        if "risk" in raw:
            r = raw["risk"]
            base = config.risk
            config.risk = RiskPenalties(
                suspicious_low_price=float(r.get("suspicious_low_price", base.suspicious_low_price)),
                price_ratio_floor=float(r.get("price_ratio_floor", base.price_ratio_floor)),
                no_photo=float(r.get("no_photo", base.no_photo)),
                short_description=float(r.get("short_description", base.short_description)),
                short_description_word_threshold=int(
                    r.get("short_description_word_threshold", base.short_description_word_threshold)
                ),
                unrecognized_price=float(r.get("unrecognized_price", base.unrecognized_price)),
                parts_or_no_docs=float(r.get("parts_or_no_docs", base.parts_or_no_docs)),
                damaged=float(r.get("damaged", base.damaged)),
            )
        if "quality" in raw:
            q = raw["quality"]
            base_q = config.quality
            config.quality = QualityBonuses(
                has_photo=float(q.get("has_photo", base_q.has_photo)),
                description_per_word=float(q.get("description_per_word", base_q.description_per_word)),
                description_word_cap=int(q.get("description_word_cap", base_q.description_word_cap)),
                has_frame_size=float(q.get("has_frame_size", base_q.has_frame_size)),
                has_year=float(q.get("has_year", base_q.has_year)),
                base=float(q.get("base", base_q.base)),
            )
        if "freshness" in raw:
            f = raw["freshness"]
            base_f = config.freshness
            config.freshness = FreshnessCurve(
                full_score_minutes=int(f.get("full_score_minutes", base_f.full_score_minutes)),
                zero_score_minutes=int(f.get("zero_score_minutes", base_f.zero_score_minutes)),
            )
        if "routing" in raw:
            rt = raw["routing"]
            base_rt = config.routing
            config.routing = RoutingThresholds(
                hot_min_score=float(rt.get("hot_min_score", base_rt.hot_min_score)),
                hot_max_age_minutes=int(rt.get("hot_max_age_minutes", base_rt.hot_max_age_minutes)),
                notify_min_score=float(rt.get("notify_min_score", base_rt.notify_min_score)),
            )

        config.validate()
        return config
