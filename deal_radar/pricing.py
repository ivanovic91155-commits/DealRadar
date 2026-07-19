from __future__ import annotations

import logging
import statistics
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol

from deal_radar.bike_identity import has_accessory_terms, has_unavailable_terms, has_used_terms, identify_bike, identify_listing, normalize_text
from deal_radar.config import RetailConfig
from deal_radar.models import BikeIdentity, Listing, RetailOffer, Valuation
from deal_radar.price_sources import PriceSource


LOGGER = logging.getLogger(__name__)


class MatchResolver(Protocol):
    def resolve(self, identity: BikeIdentity, offers: list[RetailOffer]) -> dict[str, tuple[bool, float, str]]: ...


def convert_to_czk(price: float, currency: str, rates_to_czk: dict[str, float]) -> int:
    code = currency.upper()
    if code == "CZK":
        return int(round(price))
    rate = rates_to_czk.get(code)
    if rate is None or rate <= 0:
        raise ValueError(f"Missing positive CZK conversion rate for {code}")
    return int(round(price * rate))


def discount_percent(listing_price_czk: int | None, median_price_czk: int | None) -> int | None:
    if not listing_price_czk or not median_price_czk or median_price_czk <= 0:
        return None
    return round((median_price_czk - listing_price_czk) / median_price_czk * 100)


def calculate_median(offers: list[RetailOffer]) -> int | None:
    if not offers:
        return None
    return int(round(statistics.median(offer.price_czk for offer in offers)))


def remove_price_outliers(
    offers: list[RetailOffer],
    low_ratio: float = 0.55,
    high_ratio: float = 1.8,
) -> list[RetailOffer]:
    if len(offers) < 3:
        return list(offers)
    center = statistics.median(offer.price_czk for offer in offers)
    deviations = [abs(offer.price_czk - center) for offer in offers]
    mad = statistics.median(deviations)
    filtered: list[RetailOffer] = []
    for offer in offers:
        ratio_ok = center * low_ratio <= offer.price_czk <= center * high_ratio
        robust_ok = mad == 0 or abs(offer.price_czk - center) / mad <= 3.5
        if ratio_ok and robust_ok:
            filtered.append(offer)
    return filtered


def deduplicate_offers(offers: list[RetailOffer]) -> list[RetailOffer]:
    by_url: dict[str, RetailOffer] = {}
    for offer in offers:
        current = by_url.get(offer.url)
        if current is None or offer.match_score > current.match_score:
            by_url[offer.url] = offer
    by_seller: dict[str, RetailOffer] = {}
    for offer in by_url.values():
        key = normalize_text(offer.seller)
        current = by_seller.get(key)
        if current is None or (offer.match_score, -offer.price_czk) > (current.match_score, -current.price_czk):
            by_seller[key] = offer
    return sorted(by_seller.values(), key=lambda item: (-item.match_score, item.price_czk, item.seller.casefold()))


def evaluate_offer(identity: BikeIdentity, offer: RetailOffer) -> tuple[float, str, BikeIdentity]:
    combined = f"{offer.product_name} {offer.seller}"
    candidate = identify_bike(offer.product_name)
    if offer.condition.casefold() != "new" or has_used_terms(combined):
        return 0.0, "not_new", candidate
    availability = offer.availability.casefold()
    if availability not in {"in_stock", "instock", "preorder", "limited", "unknown", ""} or has_unavailable_terms(combined):
        return 0.0, "unavailable", candidate
    if has_accessory_terms(offer.product_name):
        return 0.0, "accessory_or_part", candidate

    brand = normalize_text(identity.brand)
    candidate_brand = normalize_text(candidate.brand)
    if not brand or brand != candidate_brand:
        return 0.0, "brand_mismatch", candidate
    score = 0.35  # verified complete/new product and exact brand

    model_tokens = set(normalize_text(identity.model).split())
    candidate_tokens = set(normalize_text(candidate.model).split())
    if not model_tokens or not candidate_tokens:
        return 0.0, "model_missing", candidate
    if model_tokens == candidate_tokens:
        score += 0.45
    else:
        overlap = len(model_tokens & candidate_tokens) / len(model_tokens | candidate_tokens)
        if overlap < 0.75 or not model_tokens.issubset(candidate_tokens):
            return 0.0, "model_mismatch", candidate
        score += 0.30 * overlap

    if identity.generation:
        if candidate.generation and normalize_text(identity.generation) != normalize_text(candidate.generation):
            return 0.0, "generation_mismatch", candidate
        if candidate.generation:
            score += 0.12
    if identity.model_year:
        if candidate.model_year and identity.model_year != candidate.model_year:
            score = min(score, 0.69)
        elif candidate.model_year:
            score += 0.10
    if identity.wheel_size:
        if candidate.wheel_size and identity.wheel_size != candidate.wheel_size:
            return 0.0, "wheel_size_mismatch", candidate
        if candidate.wheel_size:
            score += 0.08
    if identity.trim:
        if candidate.trim and normalize_text(identity.trim) != normalize_text(candidate.trim):
            return 0.0, "trim_mismatch", candidate
        if candidate.trim:
            score += 0.05
    if identity.electric is not None and candidate.electric is not None:
        if identity.electric != candidate.electric:
            return 0.0, "electric_mismatch", candidate
        score += 0.05
    if identity.audience and candidate.audience:
        if identity.audience != candidate.audience:
            return 0.0, "audience_mismatch", candidate
        score += 0.05

    return round(min(score, 1.0), 3), "matched", candidate


class NewBikePriceService:
    def __init__(
        self,
        config: RetailConfig,
        sources: list[PriceSource],
        resolver: MatchResolver | None = None,
    ) -> None:
        self.config = config
        self.sources = sources
        self.resolver = resolver

    def identify(self, listing: Listing) -> BikeIdentity:
        return identify_listing(listing)

    def cache_ttl_hours(self, status: str) -> int:
        if status in {"success", "cached"}:
            return self.config.success_cache_hours
        if status in {"insufficient_data", "ambiguous_model", "model_discontinued", "codex_unavailable"}:
            return self.config.insufficient_cache_hours
        return self.config.error_cache_hours

    def as_cached(self, valuation: Valuation) -> Valuation:
        if valuation.status == "success" and valuation.median_price_czk is not None:
            return replace(valuation, status="cached")
        return valuation

    def find(self, listing: Listing, identity: BikeIdentity | None = None) -> Valuation:
        started = datetime.now(UTC)
        identity = identity or self.identify(listing)
        base = {
            "identified_product": identity.display_name or listing.title,
            "normalized_model_key": identity.normalized_key,
            "checked_at": started.isoformat(),
            "identity": identity,
        }
        if not identity.brand or not identity.model or identity.confidence < self.config.identity_min_confidence:
            LOGGER.info("Price search skipped: ambiguous identity %s", identity.normalized_key)
            return Valuation(
                **base,
                confidence="low" if identity.brand else "none",
                status="ambiguous_model",
                notes="Не удалось надежно определить бренд и точную модель.",
            )

        candidates: list[RetailOffer] = []
        errors: list[str] = []
        deadline = time.monotonic() + self.config.total_timeout_seconds
        timed_out = False
        for source in self.sources:
            if time.monotonic() >= deadline:
                timed_out = True
                errors.append("total timeout")
                break
            try:
                found = source.search(identity)
                candidates.extend(found)
                LOGGER.info("Price source %s returned %d candidates for %s", source.name, len(found), identity.normalized_key)
            except TimeoutError:
                errors.append(f"{source.name}: timeout")
                LOGGER.warning("Price source %s timed out for %s", source.name, identity.normalized_key)
            except Exception as exc:
                errors.append(f"{source.name}: {type(exc).__name__}")
                LOGGER.warning("Price source %s failed for %s: %s", source.name, identity.normalized_key, exc)
            if time.monotonic() > deadline:
                timed_out = True
                errors.append("total timeout")
                break

        scored: list[RetailOffer] = []
        ambiguous: list[RetailOffer] = []
        reasons: dict[str, int] = {}
        matching_unavailable = 0
        for offer in candidates:
            score, reason, candidate_identity = evaluate_offer(identity, offer)
            reasons[reason] = reasons.get(reason, 0) + 1
            enriched = replace(
                offer,
                brand=candidate_identity.brand,
                model=candidate_identity.model,
                model_year=candidate_identity.model_year,
                generation=candidate_identity.generation,
                trim=candidate_identity.trim,
                wheel_size=candidate_identity.wheel_size,
                match_score=score,
                match="exact" if score >= self.config.exact_match_threshold else "close",
                exclusion_reason="" if score >= self.config.ambiguous_match_threshold else reason,
            )
            if (
                reason == "unavailable"
                and normalize_text(candidate_identity.brand) == normalize_text(identity.brand)
                and normalize_text(candidate_identity.model) == normalize_text(identity.model)
            ):
                matching_unavailable += 1
            if score >= self.config.exact_match_threshold:
                scored.append(enriched)
            elif score >= self.config.ambiguous_match_threshold:
                ambiguous.append(enriched)

        codex_failed = False
        if ambiguous and self.resolver is not None:
            try:
                decisions = self.resolver.resolve(identity, ambiguous)
                for offer in ambiguous:
                    accepted, confidence, reason = decisions.get(offer.url, (False, 0.0, "no_decision"))
                    if accepted and confidence >= self.config.exact_match_threshold:
                        scored.append(replace(offer, match_score=confidence, match="exact", exclusion_reason=""))
                    else:
                        reasons[f"codex:{reason}"] = reasons.get(f"codex:{reason}", 0) + 1
            except Exception as exc:
                codex_failed = True
                LOGGER.warning("Codex match fallback unavailable for %s: %s", identity.normalized_key, exc)

        deduplicated = deduplicate_offers(scored)
        filtered = remove_price_outliers(
            deduplicated,
            low_ratio=self.config.outlier_low_ratio,
            high_ratio=self.config.outlier_high_ratio,
        )
        source_count = len({normalize_text(offer.seller) for offer in filtered})
        median = calculate_median(filtered) if source_count >= self.config.min_comparables else None

        if median is not None:
            status = "success"
            confidence = "high" if identity.confidence >= 0.9 and all(item.match_score >= 0.9 for item in filtered) else "medium"
        elif matching_unavailable and not scored:
            status = "model_discontinued"
            confidence = "low"
        elif codex_failed and ambiguous:
            status = "codex_unavailable"
            confidence = "low"
        elif timed_out and not scored:
            status = "timeout"
            confidence = "none"
        elif not candidates and errors:
            status = "source_error"
            confidence = "none"
        else:
            status = "insufficient_data"
            confidence = "low" if candidates else "none"

        elapsed = (datetime.now(UTC) - started).total_seconds()
        excluded = ", ".join(f"{key}={value}" for key, value in sorted(reasons.items()) if key != "matched")
        notes = f"Кандидатов: {len(candidates)}; точных после фильтрации: {source_count}."
        if excluded:
            notes += f" Исключено: {excluded}."
        if errors:
            notes += f" Ошибки источников: {', '.join(errors)}."
        LOGGER.info(
            "Price result %s: status=%s candidates=%d exact=%d median=%s elapsed=%.2fs",
            identity.normalized_key,
            status,
            len(candidates),
            source_count,
            median,
            elapsed,
        )
        return Valuation(
            **base,
            confidence=confidence,
            status=status,
            comparables=filtered[: self.config.max_offers_kept],
            median_price_czk=median,
            source_count=source_count,
            discount_percent=discount_percent(listing.price_czk, median),
            notes=notes,
        )
