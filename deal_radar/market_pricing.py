from __future__ import annotations

import hashlib
import logging
import math
import statistics
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from deal_radar.bike_identity import has_accessory_terms, identify_bike, identify_listing, normalize_text
from deal_radar.config import MarketPricingConfig
from deal_radar.duplicates import description_fingerprint, normalize_duplicate_text, text_similarity
from deal_radar.exchange_rates import ExchangeRateProvider
from deal_radar.models import BikeIdentity, Listing, MarketComparable, MarketValuation, Valuation
from deal_radar.pricing import convert_to_czk


LOGGER = logging.getLogger(__name__)


class MarketStorage(Protocol):
    def get_cached_market_valuation(self, listing: Listing) -> MarketValuation | None: ...

    def save_market_valuation(self, listing: Listing, valuation: MarketValuation) -> None: ...

    def local_market_candidates(self, listing: Listing, *, max_age_days: int) -> list[MarketComparable]: ...

    def get_market_lookup_cache(self, cache_key: str) -> tuple[str, list[MarketComparable]] | None: ...

    def save_market_lookup_cache(
        self,
        cache_key: str,
        source: str,
        market: str,
        status: str,
        items: list[MarketComparable],
        ttl_hours: int,
    ) -> None: ...

    def market_source_available(self, source: str) -> bool: ...

    def record_market_source_success(self, source: str) -> None: ...

    def record_market_source_error(
        self, source: str, error: str, *, threshold: int, cooldown_minutes: int
    ) -> int: ...

    def get_duplicate_record(self, listing: Listing): ...


class MarketSource(Protocol):
    name: str
    country: str
    request_count: int

    def search(self, identity: BikeIdentity) -> list[MarketComparable]: ...


REJECT_TERMS = (
    "deposit",
    "depozit",
    "zaloha",
    "záloha",
    "monthly payment",
    "mesicne",
    "měsíčně",
    "rent",
    "pronajem",
    "pronájem",
    "wanted",
    "koupim",
    "koupím",
    "suche",
    "gesucht",
    "gezocht",
)


def market_cache_key(identity: BikeIdentity, source: str, market: str) -> str:
    return f"market-v2|{identity.normalized_key}|family={normalize_text(identity.model_family)}|trim={normalize_text(identity.trim)}|type={identity.bike_type or '?'}|electric={identity.electric}|source={source}|market={market}"


def market_listing_fingerprint(item: MarketComparable) -> str:
    description = normalize_duplicate_text(item.description)
    if len(description) >= 40:
        text = description
    else:
        text = normalize_duplicate_text(item.title)
    payload = "|".join(
        (
            normalize_text(item.brand),
            normalize_text(item.model),
            str(item.price_czk or item.price_original or ""),
            text,
            normalize_text(item.location),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest() if payload.strip("|") else ""


def _compatible(first: object, second: object) -> bool:
    if first in {None, ""} or second in {None, ""}:
        return True
    if isinstance(first, str) and isinstance(second, str):
        return normalize_text(first) == normalize_text(second)
    return first == second


def _component_similarity(target: BikeIdentity, candidate: BikeIdentity) -> tuple[int, int]:
    fields = (
        "frame_material",
        "fork_class",
        "drivetrain_class",
        "brake_class",
        "suspension_type",
    )
    known = 0
    matches = 0
    for field_name in fields:
        first = getattr(target, field_name)
        second = getattr(candidate, field_name)
        if first and second:
            known += 1
            matches += int(normalize_text(str(first)) == normalize_text(str(second)))
    if target.travel_mm and candidate.travel_mm:
        known += 1
        matches += int(abs(target.travel_mm - candidate.travel_mm) <= 20)
    return matches, known


def match_market_comparable(
    target: BikeIdentity,
    comparable: MarketComparable,
) -> tuple[str, float, str, BikeIdentity]:
    combined = normalize_text(f"{comparable.title} {comparable.description}")
    candidate = identify_bike(comparable.title, comparable.description)
    if has_accessory_terms(comparable.title) or any(term in combined for term in REJECT_TERMS):
        return "rejected", 0.0, "part_payment_rental_or_wanted", candidate
    if target.electric is not None and candidate.electric is not None and target.electric != candidate.electric:
        return "rejected", 0.0, "electric_mismatch", candidate
    if target.audience and candidate.audience and target.audience != candidate.audience:
        return "rejected", 0.0, "audience_mismatch", candidate
    if target.bike_type and candidate.bike_type and target.bike_type != candidate.bike_type:
        return "rejected", 0.0, "bike_type_mismatch", candidate
    if (
        target.suspension_type
        and candidate.suspension_type
        and target.suspension_type != candidate.suspension_type
    ):
        return "rejected", 0.0, "suspension_type_mismatch", candidate

    same_brand = bool(target.brand and normalize_text(target.brand) == normalize_text(candidate.brand))
    same_model = bool(target.model and normalize_text(target.model) == normalize_text(candidate.model))
    generation_ok = _compatible(target.generation, candidate.generation)
    trim_ok = _compatible(target.trim, candidate.trim)
    year_delta = (
        abs(target.model_year - candidate.model_year)
        if target.model_year and candidate.model_year
        else None
    )
    if same_brand and same_model:
        if not generation_ok:
            return "rejected", 0.0, "generation_mismatch", candidate
        if not trim_ok:
            return "model_family", 0.62, "trim_differs_same_model_family", candidate
        if year_delta is not None and year_delta <= 1:
            return "exact", round(1.0 - year_delta * 0.05, 3), "exact_brand_model", candidate
        if year_delta is None:
            return "close", 0.82, "exact_model_year_unknown", candidate
        if year_delta <= 2:
            return "close", 0.78, "exact_model_close_year", candidate
        return "rejected", 0.0, "model_year_too_far", candidate

    same_family = bool(
        same_brand
        and target.model_family
        and candidate.model_family
        and normalize_text(target.model_family) == normalize_text(candidate.model_family)
    )
    if same_family and generation_ok:
        if year_delta is None or year_delta <= 3:
            return "model_family", 0.60, "same_model_family", candidate
        return "rejected", 0.0, "family_year_too_far", candidate

    component_matches, component_known = _component_similarity(target, candidate)
    if (
        target.bike_type
        and candidate.bike_type
        and target.bike_type == candidate.bike_type
        and component_known >= 2
        and component_matches / component_known >= 0.67
    ):
        return "component_class", 0.38, "similar_component_class", candidate
    if not same_brand:
        return "rejected", 0.0, "brand_mismatch", candidate
    return "rejected", 0.0, "model_mismatch", candidate


def _weighted_quantile(items: list[MarketComparable], quantile: float) -> int | None:
    priced = sorted((item for item in items if item.price_czk and item.weight > 0), key=lambda item: item.price_czk or 0)
    if not priced:
        return None
    target = sum(item.weight for item in priced) * min(1.0, max(0.0, quantile))
    current = 0.0
    for item in priced:
        current += item.weight
        if current >= target:
            return int(item.price_czk or 0)
    return int(priced[-1].price_czk or 0)


def _remove_outliers(items: list[MarketComparable]) -> tuple[list[MarketComparable], list[MarketComparable]]:
    if len(items) < 4:
        return list(items), []
    prices = [int(item.price_czk or 0) for item in items]
    center = statistics.median(prices)
    deviations = [abs(price - center) for price in prices]
    mad = statistics.median(deviations)
    kept: list[MarketComparable] = []
    removed: list[MarketComparable] = []
    for item in items:
        price = int(item.price_czk or 0)
        ratio_ok = center * 0.50 <= price <= center * 1.90
        mad_ok = mad == 0 or abs(price - center) / mad <= 3.5
        if ratio_ok and mad_ok:
            kept.append(item)
        else:
            removed.append(replace(item, exclusion_reason="statistical_price_outlier"))
    return kept or list(items), removed


def _round_price(value: float | int, config: MarketPricingConfig) -> int:
    step = (
        config.expensive_rounding_step_czk
        if value >= config.expensive_rounding_threshold_czk
        else config.cheap_rounding_step_czk
    )
    return int(round(float(value) / step) * step)


def _depreciation_coefficient(identity: BikeIdentity, config: MarketPricingConfig) -> float | None:
    if identity.model_year:
        age = max(0, datetime.now(UTC).year - identity.model_year)
        return config.depreciation_by_age.get(str(age), config.depreciation_by_age.get("6+"))
    if identity.generation:
        return config.depreciation_by_age.get("3")
    return None


class MarketPriceEngine:
    def __init__(
        self,
        config: MarketPricingConfig,
        storage: MarketStorage,
        exchange_rates: ExchangeRateProvider,
        sources: list[MarketSource],
    ) -> None:
        self.config = config
        self.storage = storage
        self.exchange_rates = exchange_rates
        self.sources = {source.name: source for source in sources}

    def market_route(self, identity: BikeIdentity) -> tuple[list[str], list[str]]:
        configured = self.config.brand_market_mapping.get(identity.brand, [])
        reasons: list[str] = []
        if configured:
            reasons.append(f"Рынки выбраны по brand_market_mapping для {identity.brand}: {', '.join(configured)}.")
            candidates = configured
        else:
            candidates = self.config.category_market_mapping.get(identity.bike_type, ["DE", "NL"])
            reasons.append(
                f"Рынки выбраны по категории {identity.bike_type or 'unknown'}: {', '.join(candidates)}."
            )
        national: list[str] = []
        include_eu = False
        for market in candidates:
            market = market.upper()
            if market == "CZ":
                continue
            if market == "EU":
                include_eu = True
            elif market in {source.country for source in self.sources.values()} and market not in national:
                national.append(market)
        national = national[: self.config.max_foreign_markets_per_model]
        if not national:
            fallback = "NL" if identity.bike_type == "city" else "DE"
            if fallback in {source.country for source in self.sources.values()}:
                national.append(fallback)
        route = ["CZ", *national]
        if include_eu or "EU" in {source.country for source in self.sources.values()}:
            route.append("EU")
        return route, reasons

    def _search_source(
        self,
        source: MarketSource,
        identity: BikeIdentity,
        market: str,
        *,
        read_only: bool,
    ) -> tuple[list[MarketComparable], bool, str]:
        key = market_cache_key(identity, source.name, market)
        cached = self.storage.get_market_lookup_cache(key)
        if cached is not None:
            status, items = cached
            return items, True, status
        if not self.storage.market_source_available(source.name):
            return [], True, "circuit_open"
        try:
            items = source.search(identity)
            status = "success" if items else "not_found"
            if not read_only:
                ttl = self.config.cache_ttl_hours["low_confidence" if items else "not_found"]
                self.storage.save_market_lookup_cache(key, source.name, market, status, items, ttl)
                self.storage.record_market_source_success(source.name)
            return items, False, status
        except Exception as exc:
            LOGGER.warning("Used-market source %s failed: %s", source.name, exc)
            if not read_only:
                self.storage.save_market_lookup_cache(
                    key,
                    source.name,
                    market,
                    "source_error",
                    [],
                    self.config.cache_ttl_hours["source_error"],
                )
                self.storage.record_market_source_error(
                    source.name,
                    f"{type(exc).__name__}: {exc}",
                    threshold=self.config.circuit_breaker_errors,
                    cooldown_minutes=self.config.circuit_breaker_cooldown_minutes,
                )
            return [], False, "source_error"

    def _enrich_and_match(
        self,
        identity: BikeIdentity,
        items: list[MarketComparable],
    ) -> tuple[list[MarketComparable], list[MarketComparable]]:
        accepted: list[MarketComparable] = []
        rejected: list[MarketComparable] = []
        for item in items:
            match_type, score, reason, candidate = match_market_comparable(identity, item)
            enriched = replace(
                item,
                brand=candidate.brand,
                model=candidate.model,
                model_family=candidate.model_family,
                generation=candidate.generation,
                trim=candidate.trim,
                year=candidate.model_year,
                frame_size=candidate.frame_size,
                wheel_size=candidate.wheel_size,
                bike_type=candidate.bike_type,
                is_ebike=candidate.electric,
                suspension_type=candidate.suspension_type,
                frame_material=candidate.frame_material,
                fork_class=candidate.fork_class,
                drivetrain_class=candidate.drivetrain_class,
                brake_class=candidate.brake_class,
                travel_mm=candidate.travel_mm,
                match_type=match_type,
                match_score=score,
                exclusion_reason="" if match_type != "rejected" else reason,
            )
            enriched.listing_fingerprint = enriched.listing_fingerprint or market_listing_fingerprint(enriched)
            (accepted if match_type != "rejected" else rejected).append(enriched)
        return accepted, rejected

    def _exclude_self_and_deduplicate(
        self,
        listing: Listing,
        items: list[MarketComparable],
    ) -> tuple[list[MarketComparable], int, int, list[MarketComparable]]:
        duplicate_record = self.storage.get_duplicate_record(listing)
        target_group = str(duplicate_record["group_id"]) if duplicate_record else ""
        target_identity = identify_listing(listing)
        target_description = listing.description
        target_description_hash = description_fingerprint(target_description)
        target_image = ""
        if listing.image_url:
            target_image = hashlib.sha256(listing.image_url.split("?", 1)[0].casefold().encode("utf-8")).hexdigest()
        filtered: list[MarketComparable] = []
        rejected: list[MarketComparable] = []
        source_copies = 0
        for item in items:
            same_key = item.source == listing.source and item.source_listing_id == listing.external_id
            same_url = bool(item.url and item.url == listing.url)
            same_group = bool(target_group and item.duplicate_group_id == target_group)
            same_identity = bool(
                normalize_text(item.brand) == normalize_text(target_identity.brand)
                and (
                    normalize_text(item.model) == normalize_text(target_identity.model)
                    or (
                        target_identity.model_family
                        and normalize_text(item.model_family) == normalize_text(target_identity.model_family)
                    )
                )
            )
            same_description = bool(
                same_identity
                and target_description_hash
                and item.listing_fingerprint == target_description_hash
                and len(normalize_duplicate_text(target_description)) >= 80
            )
            target_price = listing.price_czk or listing.price_amount
            same_price = bool(
                target_price
                and item.price_czk
                and abs(float(target_price) - float(item.price_czk)) / max(float(target_price), float(item.price_czk))
                <= 0.02
            )
            near_description = bool(
                same_identity
                and same_price
                and len(normalize_duplicate_text(target_description)) >= 80
                and len(normalize_duplicate_text(item.description)) >= 80
                and text_similarity(target_description, item.description) >= 0.95
            )
            same_image = bool(target_image and item.image_fingerprint == target_image)
            if same_key or same_url or same_group or same_description or near_description or same_image:
                source_copies += 1
                rejected.append(replace(item, exclusion_reason="copy_of_valued_listing"))
            else:
                filtered.append(item)

        groups: list[list[MarketComparable]] = []
        for item in filtered:
            placed = False
            for group in groups:
                first = group[0]
                same_group = bool(item.duplicate_group_id and item.duplicate_group_id == first.duplicate_group_id)
                same_url = bool(item.url and item.url == first.url)
                same_fingerprint = bool(
                    item.listing_fingerprint and item.listing_fingerprint == first.listing_fingerprint
                )
                same_image = bool(item.image_fingerprint and item.image_fingerprint == first.image_fingerprint)
                near_text = bool(
                    item.source != first.source
                    and item.price_czk == first.price_czk
                    and normalize_text(item.brand) == normalize_text(first.brand)
                    and normalize_text(item.model) == normalize_text(first.model)
                    and text_similarity(item.description, first.description) >= 0.95
                )
                if same_group or same_url or same_fingerprint or same_image or near_text:
                    group.append(item)
                    placed = True
                    break
            if not placed:
                groups.append([item])
        unique: list[MarketComparable] = []
        duplicates_removed = 0
        for group in groups:
            group.sort(key=lambda item: (-item.match_score, item.country != "CZ", item.price_czk or math.inf))
            unique.append(group[0])
            duplicates_removed += len(group) - 1
            rejected.extend(replace(item, exclusion_reason="duplicate_comparable") for item in group[1:])
        return unique, duplicates_removed, source_copies, rejected

    def _weight(self, item: MarketComparable) -> float:
        geography = "cz" if item.country == "CZ" else "foreign"
        base = self.config.weights[f"{item.match_type}_{geography}"]
        freshness = 1.0
        if item.published_at:
            try:
                published = datetime.fromisoformat(item.published_at)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=UTC)
                age_days = max(0, (datetime.now(UTC) - published.astimezone(UTC)).days)
                freshness = 1.0 if age_days <= 30 else 0.9 if age_days <= 90 else 0.75
            except ValueError:
                pass
        return round(base * max(0.35, item.match_score) * freshness, 4)

    def find(
        self,
        listing: Listing,
        identity: BikeIdentity | None = None,
        *,
        new_valuation: Valuation | None = None,
        read_only: bool = False,
        force: bool = False,
    ) -> MarketValuation:
        identity = identity or identify_listing(listing)
        if not force:
            cached = self.storage.get_cached_market_valuation(listing)
            if cached is not None:
                return cached
        now = datetime.now(UTC)
        route, route_reasons = self.market_route(identity)
        local_candidates = self.storage.local_market_candidates(
            listing,
            max_age_days=self.config.max_comparable_age_days,
        )
        # The canonical row being valued lives in the local listings table. Drop
        # that row before remote results are merged; a remote copy of it must still
        # be counted in diagnostics as an excluded source copy.
        raw: list[MarketComparable] = [
            item
            for item in local_candidates
            if not (item.source == listing.source and item.source_listing_id == listing.external_id)
        ]
        queried = ["CZ-local-sqlite"]
        cache_hits = 0
        cache_misses = 0
        http_requests: dict[str, int] = {}
        warnings: list[str] = []
        source_statuses: list[str] = []
        deadline = time.monotonic() + self.config.total_timeout_seconds

        def qualified_count(values: list[MarketComparable]) -> int:
            accepted, _ = self._enrich_and_match(identity, values)
            unique, _, _, _ = self._exclude_self_and_deduplicate(listing, accepted)
            return len(unique)

        for market in route:
            if market != "CZ" and qualified_count(raw) >= self.config.minimum_unique_comparables_before_fallback:
                break
            if time.monotonic() >= deadline:
                warnings.append("Достигнут общий лимит времени внешнего поиска.")
                break
            market_sources = [
                source
                for source in self.sources.values()
                if source.country == market and source.name in self.config.sources
            ]
            if not market_sources:
                if market != "CZ":
                    route_reasons.append(f"Для рынка {market} нет подключённого безопасного источника.")
                continue
            for source in market_sources:
                before = source.request_count
                found, cache_hit, status = self._search_source(
                    source, identity, market, read_only=read_only
                )
                queried.append(source.name)
                source_statuses.append(f"{source.name}:{status}")
                raw.extend(found)
                cache_hits += int(cache_hit)
                cache_misses += int(not cache_hit)
                requests = max(0, source.request_count - before)
                if requests:
                    http_requests[source.name] = http_requests.get(source.name, 0) + requests

        accepted, rejected = self._enrich_and_match(identity, raw)
        needs_currency = any(item.currency.upper() != "CZK" for item in accepted)
        rate_result = self.exchange_rates.get(read_only=read_only) if needs_currency else None
        if rate_result:
            warnings.extend(rate_result.warnings)
            if rate_result.http_requests:
                http_requests["cnb_exchange_rates"] = rate_result.http_requests
        rates = rate_result.rates_to_czk if rate_result else {"CZK": 1.0}
        converted: list[MarketComparable] = []
        for item in accepted:
            try:
                price_czk = convert_to_czk(float(item.price_original or item.price_czk or 0), item.currency, rates)
            except ValueError:
                rejected.append(replace(item, exclusion_reason="currency_rate_missing"))
                continue
            adjustment = self.config.country_market_adjustment.get(item.country, 1.0)
            converted.append(replace(item, price_czk=int(round(price_czk * adjustment))))

        unique, duplicates_removed, source_copies, duplicate_rejected = self._exclude_self_and_deduplicate(
            listing, converted
        )
        rejected.extend(duplicate_rejected)
        filtered, outliers = _remove_outliers(unique)
        rejected.extend(outliers)
        weighted = [replace(item, weight=self._weight(item)) for item in filtered]
        # A single cross-brand component-class match is too weak to price a bike.
        # Keep it visible in diagnostics, but require at least two such comparables
        # before it can become the sole basis of a valuation.
        if len(weighted) == 1 and weighted[0].match_type == "component_class":
            rejected.append(replace(weighted[0], exclusion_reason="insufficient_component_class_sample"))
            weighted = []
        market_price = _weighted_quantile(weighted, 0.50)
        method = "weighted_median"
        status = "market_price_found"
        confidence = "low"

        if market_price is None:
            coefficient = _depreciation_coefficient(identity, self.config)
            if new_valuation and new_valuation.median_price_czk and coefficient:
                market_price = _round_price(new_valuation.median_price_czk * coefficient, self.config)
                method = "new_price_depreciation"
                status = "depreciation_estimate"
                warnings.append("Used-аналоги не найдены; применена приблизительная амортизация новой цены.")
            elif accepted and not converted:
                status = "currency_error"
                method = "none"
            elif raw and source_copies and not unique:
                status = "duplicate_only_results"
                method = "none"
            elif not identity.brand or not identity.model:
                status = "ambiguous_model"
                method = "none"
            elif not raw and any(value.endswith("source_error") for value in source_statuses):
                status = "source_unavailable"
                method = "none"
            else:
                status = "no_matching_comparables" if raw else "insufficient_comparables"
                method = "none"

        count = len(weighted)
        country_counts: dict[str, int] = {}
        country_medians: dict[str, int] = {}
        for country in sorted({item.country for item in weighted}):
            country_items = [item for item in weighted if item.country == country]
            country_counts[country] = len(country_items)
            median = _weighted_quantile(country_items, 0.50)
            if median is not None:
                country_medians[country] = median

        if market_price is not None and count:
            spread = (max(item.price_czk or 0 for item in weighted) - min(item.price_czk or 0 for item in weighted)) / market_price
            has_cz = bool(country_counts.get("CZ"))
            if count >= 5 and has_cz and spread <= 0.35:
                confidence = "high"
            elif count >= 3 or (count >= 2 and has_cz and len(country_counts) > 1):
                confidence = "medium" if spread <= 0.55 else "low"
            else:
                confidence = "low"
            if not has_cz:
                confidence = "low"
                status = "foreign_only_estimate"
                warnings.append("В Чехии подходящие аналоги не найдены; оценка построена по зарубежному рынку.")
            if weighted and all(item.match_type == "component_class" for item in weighted):
                confidence = "low"
                status = "component_class_estimate"
            elif weighted and all(item.match_type == "model_family" for item in weighted):
                confidence = "low"
                status = "market_price_low_confidence"
            elif count <= 2 and status != "foreign_only_estimate":
                status = "market_price_low_confidence"

            cz_median = country_medians.get("CZ")
            foreign_values = [value for country, value in country_medians.items() if country != "CZ"]
            if cz_median and foreign_values:
                foreign_median = int(statistics.median(foreign_values))
                disagreement = abs(cz_median - foreign_median) / max(cz_median, foreign_median)
                if disagreement > self.config.foreign_market_disagreement_threshold:
                    confidence = "medium" if confidence == "high" else "low"
                    warnings.append(
                        f"Медианы Чехии и зарубежных рынков расходятся на {round(disagreement * 100)}%."
                    )

        if market_price is not None:
            if count >= 3:
                low = _round_price(_weighted_quantile(weighted, 0.25) or market_price, self.config)
                high = _round_price(_weighted_quantile(weighted, 0.75) or market_price, self.config)
            elif count == 2:
                low = _round_price(min(item.price_czk or market_price for item in weighted) * 0.95, self.config)
                high = _round_price(max(item.price_czk or market_price for item in weighted) * 1.05, self.config)
            else:
                low = _round_price(market_price * 0.85, self.config)
                high = _round_price(market_price * 1.15, self.config)
            market_price = _round_price(market_price, self.config)
            quick = _round_price(market_price * (1 - self.config.quick_sale_discount), self.config)
        else:
            low = high = quick = None

        if confidence == "high":
            ttl = self.config.cache_ttl_hours["high_confidence"]
        elif market_price is not None:
            ttl = self.config.cache_ttl_hours["low_confidence"]
        else:
            ttl = self.config.cache_ttl_hours["not_found"]
        expires_at = (now + timedelta(hours=ttl)).isoformat()
        valuation = MarketValuation(
            listing_source=listing.source,
            listing_external_id=listing.external_id,
            market_price_czk=market_price,
            quick_sale_price_czk=quick,
            price_low_czk=low,
            price_high_czk=high,
            confidence=confidence,
            valuation_method=method,
            status=status,
            comparables_total=len(raw),
            comparables_unique=len(weighted),
            exact_comparables=sum(item.match_type == "exact" for item in weighted),
            close_comparables=sum(item.match_type == "close" for item in weighted),
            family_comparables=sum(item.match_type == "model_family" for item in weighted),
            component_comparables=sum(item.match_type == "component_class" for item in weighted),
            cz_comparables=country_counts.get("CZ", 0),
            foreign_comparables=sum(value for key, value in country_counts.items() if key != "CZ"),
            countries_used=sorted(country_counts),
            sources_used=sorted({item.source for item in weighted}),
            country_counts=country_counts,
            country_medians_czk=country_medians,
            country_adjustments={
                country: self.config.country_market_adjustment.get(country, 1.0)
                for country in country_counts
            },
            warnings=warnings,
            comparables=weighted,
            rejected_comparables=rejected[:100],
            duplicates_removed=duplicates_removed,
            source_copies_excluded=source_copies,
            markets_queried=queried,
            market_reasons=route_reasons + source_statuses,
            exchange_rates_to_czk={key: rates[key] for key in ("CZK", "EUR", "PLN", "CHF") if key in rates},
            exchange_rate_source=rate_result.source if rate_result else "",
            exchange_rate_date=rate_result.valid_date if rate_result else "",
            cache_hits=cache_hits + int(rate_result.cache_used if rate_result else False),
            cache_misses=cache_misses + int(bool(rate_result and not rate_result.cache_used)),
            http_requests=http_requests,
            calculated_at=now.isoformat(),
            expires_at=expires_at,
        )
        if not read_only:
            self.storage.save_market_valuation(listing, valuation)
        LOGGER.info(
            "Market Price Engine: %s status=%s unique=%d price=%s confidence=%s cache=%d/%d",
            listing.key,
            valuation.status,
            valuation.comparables_unique,
            valuation.market_price_czk,
            valuation.confidence,
            valuation.cache_hits,
            valuation.cache_misses,
        )
        return valuation
