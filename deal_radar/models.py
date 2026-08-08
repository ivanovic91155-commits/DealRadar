from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True)
class Listing:
    source: str
    external_id: str
    title: str
    description: str
    url: str
    profile: str
    price_czk: int | None = None
    location: str | None = None
    published_at: datetime | None = None
    image_url: str | None = None
    price_amount: int | None = None
    currency: str = "CZK"
    raw_price_text: str = ""
    price_status: str = ""
    price_origin: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}:{self.external_id}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["published_at"] = self.published_at.isoformat() if self.published_at else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Listing":
        copy = dict(data)
        if copy.get("published_at"):
            copy["published_at"] = datetime.fromisoformat(copy["published_at"])
        return cls(**copy)


@dataclass(slots=True)
class BikeIdentity:
    brand: str = ""
    model: str = ""
    generation: str = ""
    model_year: int | None = None
    trim: str = ""
    wheel_size: str = ""
    frame_size: str = ""
    bike_type: str = ""
    electric: bool | None = None
    audience: str = ""
    model_family: str = ""
    suspension_type: str = ""
    frame_material: str = ""
    fork_class: str = ""
    drivetrain_class: str = ""
    brake_class: str = ""
    travel_mm: int | None = None
    confidence: float = 0.0
    # Модель считается подтверждённой только при совпадении с каталогом.
    # model_source: "catalog" | "catalog_fuzzy" | "tail" | "" (устаревшие записи).
    model_confirmed: bool = False
    model_source: str = ""

    @property
    def display_name(self) -> str:
        parts = [self.brand, self.model, self.generation, str(self.model_year or ""), self.wheel_size]
        return " ".join(part for part in parts if part).strip()

    @property
    def normalized_key(self) -> str:
        def slug(value: str) -> str:
            clean = "".join(char.casefold() if char.isalnum() else "-" for char in value)
            return "-".join(part for part in clean.split("-") if part)

        return "|".join(
            [
                slug(self.brand) or "?",
                slug(self.model) or "?",
                slug(self.generation) or "?",
                str(self.model_year or "?"),
                slug(self.wheel_size) or "?",
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BikeIdentity":
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass(slots=True)
class RetailOffer:
    seller: str
    product_name: str
    price_czk: int
    url: str
    match: str = "close"
    brand: str = ""
    model: str = ""
    model_year: int | None = None
    generation: str = ""
    trim: str = ""
    wheel_size: str = ""
    currency: str = "CZK"
    original_price: float | None = None
    availability: str = "in_stock"
    condition: str = "new"
    match_score: float = 0.0
    collected_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    exclusion_reason: str = ""
    store_domain: str = ""
    acceptance_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetailOffer":
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})


# Compatibility name for caches and integrations created by the first prototype.
Comparable = RetailOffer


@dataclass(slots=True)
class Valuation:
    identified_product: str
    confidence: str
    comparables: list[RetailOffer] = field(default_factory=list)
    median_price_czk: int | None = None
    notes: str = ""
    status: str = "insufficient_data"
    normalized_model_key: str = ""
    source_count: int = 0
    discount_percent: int | None = None
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    identity: BikeIdentity | None = None
    offer_count: int = 0
    independent_source_count: int = 0
    new_price_confidence: str = "insufficient"
    new_price_confidence_reasons: list[str] = field(default_factory=list)
    cache_used: bool = False
    rejected_offers: list[RetailOffer] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Valuation":
        copy = dict(data)
        copy["comparables"] = [RetailOffer.from_dict(item) for item in copy.get("comparables", [])]
        copy["rejected_offers"] = [RetailOffer.from_dict(item) for item in copy.get("rejected_offers", [])]
        if isinstance(copy.get("identity"), dict):
            copy["identity"] = BikeIdentity.from_dict(copy["identity"])
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in copy.items() if key in allowed})


@dataclass(slots=True)
class UsedComparable:
    source: str
    external_id: str
    title: str
    url: str
    price_czk: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UsedComparable":
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass(slots=True)
class UsedComparables:
    count: int = 0
    minimum_price_czk: int | None = None
    maximum_price_czk: int | None = None
    median_price_czk: int | None = None
    items: list[UsedComparable] = field(default_factory=list)
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    confidence: str = "insufficient"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UsedComparables":
        copy = dict(data)
        copy["items"] = [UsedComparable.from_dict(item) for item in copy.get("items", [])]
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in copy.items() if key in allowed})


@dataclass(slots=True)
class MarketComparable:
    source: str
    source_listing_id: str
    url: str
    country: str
    title: str
    description: str = ""
    price_original: float | None = None
    currency: str = "CZK"
    price_czk: int | None = None
    brand: str = ""
    model: str = ""
    model_family: str = ""
    generation: str = ""
    trim: str = ""
    year: int | None = None
    frame_size: str = ""
    wheel_size: str = ""
    bike_type: str = ""
    is_ebike: bool | None = None
    suspension_type: str = ""
    frame_material: str = ""
    fork_class: str = ""
    drivetrain_class: str = ""
    brake_class: str = ""
    travel_mm: int | None = None
    match_type: str = "unmatched"
    match_score: float = 0.0
    listing_fingerprint: str = ""
    image_fingerprint: str = ""
    duplicate_group_id: str = ""
    published_at: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    is_active: bool = True
    image_url: str = ""
    seller: str = ""
    location: str = ""
    weight: float = 0.0
    exclusion_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MarketComparable":
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass(slots=True)
class MarketValuation:
    listing_source: str
    listing_external_id: str
    market_price_czk: int | None = None
    quick_sale_price_czk: int | None = None
    price_low_czk: int | None = None
    price_high_czk: int | None = None
    confidence: str = "low"
    valuation_method: str = "none"
    status: str = "insufficient_comparables"
    comparables_total: int = 0
    comparables_unique: int = 0
    exact_comparables: int = 0
    close_comparables: int = 0
    family_comparables: int = 0
    component_comparables: int = 0
    cz_comparables: int = 0
    foreign_comparables: int = 0
    countries_used: list[str] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)
    country_counts: dict[str, int] = field(default_factory=dict)
    country_medians_czk: dict[str, int] = field(default_factory=dict)
    country_adjustments: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    comparables: list[MarketComparable] = field(default_factory=list)
    rejected_comparables: list[MarketComparable] = field(default_factory=list)
    duplicates_removed: int = 0
    source_copies_excluded: int = 0
    markets_queried: list[str] = field(default_factory=list)
    market_reasons: list[str] = field(default_factory=list)
    exchange_rates_to_czk: dict[str, float] = field(default_factory=dict)
    exchange_rate_source: str = ""
    exchange_rate_date: str = ""
    cache_used: bool = False
    cache_hits: int = 0
    cache_misses: int = 0
    http_requests: dict[str, int] = field(default_factory=dict)
    calculated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    expires_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MarketValuation":
        copy = dict(data)
        copy["comparables"] = [
            MarketComparable.from_dict(item) for item in copy.get("comparables", [])
        ]
        copy["rejected_comparables"] = [
            MarketComparable.from_dict(item) for item in copy.get("rejected_comparables", [])
        ]
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in copy.items() if key in allowed})

    def as_used_comparables(self) -> UsedComparables:
        priced = [item for item in self.comparables if item.price_czk]
        return UsedComparables(
            count=self.comparables_unique,
            minimum_price_czk=self.price_low_czk,
            maximum_price_czk=self.price_high_czk,
            median_price_czk=self.market_price_czk,
            items=[
                UsedComparable(
                    source=item.source,
                    external_id=item.source_listing_id,
                    title=item.title,
                    url=item.url,
                    price_czk=int(item.price_czk or 0),
                )
                for item in priced[:5]
            ],
            confidence=self.confidence if self.market_price_czk is not None else "insufficient",
        )


@dataclass(slots=True)
class DealCosts:
    acquisition_costs_czk: float = 0.0
    logistics_costs_czk: float = 0.0
    platform_fees_czk: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DealCosts":
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass(slots=True)
class DealEvaluation:
    listing_source: str
    listing_external_id: str
    algorithm_version: str
    status: str
    purchase_price_czk: float | None = None
    acquisition_costs_czk: float = 0.0
    logistics_costs_czk: float = 0.0
    platform_fees_czk: float = 0.0
    base_investment_czk: float | None = None
    risk_reserve_percent: float = 10.0
    risk_reserve_czk: float | None = None
    total_investment_czk: float | None = None
    market_median_czk: float | None = None
    quick_sale_price_czk: float | None = None
    net_profit_czk: float | None = None
    roi_percent: float | None = None
    liquidity_score: int | None = None
    liquidity_level: str = "unknown"
    estimated_days_to_sell: int | None = None
    confidence_score: int | None = None
    confidence_level: str = "low"
    condition: str = "unknown"
    market_scope: str = "unknown"
    comparable_count: int = 0
    source_count: int = 0
    valuation_status: str = "missing"
    market_valuation_ref: str = ""
    deal_score: float = 0.0
    score_components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    explanation: str = ""
    manual_review_question: str = ""
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    input_fingerprint: str = ""
    calculated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DealEvaluation":
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass(slots=True)
class ListingAnalysis:
    preliminary_priority_score: int
    priority_class: str
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    analysis_confidence: str = "low"
    identity: BikeIdentity | None = None
    valuation: Valuation | None = None
    used_comparables: UsedComparables | None = None
    cache_used: bool = False
    analyzed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    notification_status: str = "awaiting_analysis"
    notification_reason: str = ""
    duplicate_alternatives: list[dict[str, str]] = field(default_factory=list)
    market_valuation: MarketValuation | None = None
    deal_evaluation: DealEvaluation | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ListingAnalysis":
        copy = dict(data)
        if isinstance(copy.get("identity"), dict):
            copy["identity"] = BikeIdentity.from_dict(copy["identity"])
        if isinstance(copy.get("valuation"), dict):
            copy["valuation"] = Valuation.from_dict(copy["valuation"])
        if isinstance(copy.get("used_comparables"), dict):
            copy["used_comparables"] = UsedComparables.from_dict(copy["used_comparables"])
        if isinstance(copy.get("market_valuation"), dict):
            copy["market_valuation"] = MarketValuation.from_dict(copy["market_valuation"])
        if isinstance(copy.get("deal_evaluation"), dict):
            copy["deal_evaluation"] = DealEvaluation.from_dict(copy["deal_evaluation"])
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in copy.items() if key in allowed})
