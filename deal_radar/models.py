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
    confidence: float = 0.0

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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Valuation":
        copy = dict(data)
        copy["comparables"] = [RetailOffer.from_dict(item) for item in copy.get("comparables", [])]
        if isinstance(copy.get("identity"), dict):
            copy["identity"] = BikeIdentity.from_dict(copy["identity"])
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in copy.items() if key in allowed})
