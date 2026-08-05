from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterable
from datetime import UTC, datetime

from deal_radar.config import AppConfig
from deal_radar.bike_identity import identify_listing
from deal_radar.deal_scoring import DealEvaluator, select_deal_notifications
from deal_radar.exchange_rates import ExchangeRateProvider
from deal_radar.http import get_bytes
from deal_radar.market_pricing import MarketPriceEngine
from deal_radar.market_sources import (
    BazosCzechMarketSource,
    BuycycleMarketSource,
    KleinanzeigenMarketSource,
    MarktplaatsMarketSource,
    UsedMarketSource,
)
from deal_radar.models import Listing, ListingAnalysis, MarketValuation
from deal_radar.price_sources import PriceSource, ZboziPriceSource
from deal_radar.pricing import NewBikePriceService
from deal_radar.pricing import valuation_cache_key
from deal_radar.priority import build_analysis, dynamic_lookup_budget, select_lookup_candidates, select_notifications
from deal_radar.sources.bazos import BazosSource
from deal_radar.sources.cyklobazar import CyklobazarSource, parse_detail_description
from deal_radar.storage import Storage
from deal_radar.telegram import TelegramClient, format_czk


LOGGER = logging.getLogger(__name__)


def _sort_key(listing: Listing) -> tuple[bool, datetime]:
    return listing.published_at is not None, listing.published_at or datetime.min


def _deduplicate(listings: Iterable[Listing]) -> list[Listing]:
    unique: dict[str, Listing] = {}
    for listing in listings:
        unique.setdefault(listing.key, listing)
    return sorted(unique.values(), key=_sort_key, reverse=True)


class DealRadarService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.storage = Storage(config.database_path)
        self.sources = [BazosSource(profile, config.request_timeout_seconds) for profile in config.profiles]
        self.sources.extend(
            CyklobazarSource(
                profile,
                config.request_timeout_seconds,
                existing_listing=lambda external_id: self.storage.get_listing("cyklobazar", external_id),
            )
            for profile in config.cyklobazar_profiles
            if profile.enabled
        )
        # Backoff по источникам объявлений: label -> (paused_until_monotonic, delay).
        # При 403/429 источник ставится на паузу с экспоненциальным ростом, чтобы
        # не долбить площадку и не спровоцировать блокировку.
        self._source_backoff: dict[str, tuple[float, float]] = {}

    def close(self) -> None:
        self.storage.close()

    @staticmethod
    def _is_rate_limited(exc: Exception) -> bool:
        text = str(exc)
        return "403" in text or "429" in text

    def _next_interval(self) -> float:
        """Интервал до следующего опроса с небольшим случайным джиттером,
        чтобы запросы не били строго по расписанию (естественнее для площадки)."""
        base = self.config.poll_interval_seconds
        jitter = self.config.poll_jitter_seconds
        if jitter <= 0:
            return float(base)
        return base + random.uniform(-jitter, jitter)

    def fetch_all(self) -> list[Listing]:
        results: list[Listing] = []
        errors: list[str] = []
        successful_sources = 0
        now = time.monotonic()
        for source in self.sources:
            label = getattr(source, "label", None) or getattr(source.profile, "name", "marketplace")
            paused = self._source_backoff.get(label)
            if paused and now < paused[0]:
                LOGGER.info("Profile %s on backoff, skipping this cycle", label)
                continue
            try:
                fetched = source.fetch()
                successful_sources += 1
                self._source_backoff.pop(label, None)  # успех — снимаем паузу
                LOGGER.info("Profile %s: %d matching listings", label, len(fetched))
                results.extend(fetched)
            except Exception as exc:  # keep other profiles alive
                LOGGER.exception("Profile %s failed", label)
                errors.append(f"{label}: {exc}")
                if self._is_rate_limited(exc):
                    prev_delay = self._source_backoff.get(label, (0.0, 0.0))[1]
                    base = self.config.poll_interval_seconds
                    delay = min(
                        max(base, prev_delay * 2 if prev_delay else base),
                        self.config.source_backoff_max_seconds,
                    )
                    self._source_backoff[label] = (time.monotonic() + delay, delay)
                    LOGGER.warning(
                        "Profile %s rate-limited (403/429); backing off %.0f min",
                        label, delay / 60,
                    )
        if successful_sources == 0 and errors:
            raise RuntimeError("All marketplace profiles failed: " + "; ".join(errors))
        return _deduplicate(results)

    def preview(self, limit: int = 10) -> list[Listing]:
        listings = self.fetch_all()
        for listing in listings[:limit]:
            print(
                f"[{listing.external_id}] {listing.title} | {format_czk(listing.price_czk)} | "
                f"{listing.location or '-'} | {listing.url}"
            )
        return listings

    def diagnose_market(
        self,
        *,
        limit: int = 5,
        send_telegram: bool = False,
        write_state: bool = False,
        force: bool = False,
    ) -> list[tuple[Listing, ListingAnalysis]]:
        finder = self._market_finder()
        if finder is None:
            raise RuntimeError("Market Price Engine is disabled")
        telegram = self._telegram() if send_telegram else None
        results: list[tuple[Listing, ListingAnalysis]] = []
        for listing in self.storage.recent_active_listings(limit=max(limit * 4, 20)):
            identity = identify_listing(listing)
            if not identity.brand or not identity.model:
                continue
            new_valuation = self.storage.get_cached_valuation(valuation_cache_key(identity))
            market = finder.find(
                listing,
                identity,
                new_valuation=new_valuation,
                read_only=not write_state,
                force=force,
            )
            analysis = build_analysis(
                listing,
                self.config.priority,
                identity=identity,
                valuation=new_valuation,
                used_comparables=market.as_used_comparables(),
                cache_used=market.cache_used,
            )
            analysis.market_valuation = market
            analysis.duplicate_alternatives = self.storage.duplicate_alternatives(listing)
            if write_state:
                self.storage.save_analysis(listing, analysis)
            if telegram is not None:
                telegram.send_listing(
                    listing,
                    retail_enabled=False,
                    diagnostic_header="stage_2_1",
                    analysis=analysis,
                )
            results.append((listing, analysis))
            if len(results) >= limit:
                break
        if len(results) < limit:
            raise RuntimeError(f"Only {len(results)} active identifiable listings available for {limit} diagnostics")
        return results

    def _telegram(self) -> TelegramClient:
        return TelegramClient(
            self.config.telegram.bot_token,
            self.config.telegram.chat_id,
            timeout=self.config.request_timeout_seconds,
        )

    def _retail_finder(self) -> NewBikePriceService | None:
        if not self.config.retail.enabled:
            return None
        sources: list[PriceSource] = []
        if "zbozi" in self.config.retail.sources:
            sources.append(
                ZboziPriceSource(
                    timeout=self.config.retail.source_timeout_seconds,
                    max_queries=self.config.retail.max_queries_per_source,
                    max_product_pages=self.config.retail.max_product_pages,
                    max_parallel_requests=self.config.retail.max_parallel_requests,
                )
            )
        if self.config.retail.codex_enabled:
            LOGGER.warning("DEAL_RADAR_CODEX_ENABLED is ignored in stage 1.2; runtime AI is disabled")
        return NewBikePriceService(self.config.retail, sources, resolver=None)

    def _market_finder(self) -> MarketPriceEngine | None:
        config = self.config.market_pricing
        if not config.enabled:
            return None
        source_types = {
            "bazos_cz": BazosCzechMarketSource,
            "kleinanzeigen_de": KleinanzeigenMarketSource,
            "marktplaats_nl": MarktplaatsMarketSource,
            "buycycle_eu": BuycycleMarketSource,
        }
        sources: list[UsedMarketSource] = []
        for name in config.sources:
            source_type = source_types[name]
            sources.append(
                source_type(
                    timeout=config.source_timeout_seconds,
                    max_results=config.max_results_per_source,
                )
            )
        exchange_rates = ExchangeRateProvider(
            self.storage,
            config.exchange_rate_url,
            ttl_hours=config.cache_ttl_hours["exchange_rate"],
            timeout=config.source_timeout_seconds,
        )
        return MarketPriceEngine(config, self.storage, exchange_rates, sources)

    def _duplicate_detail_description(self, listing: Listing) -> str:
        if listing.source != "cyklobazar":
            return ""
        detail = get_bytes(
            listing.url,
            timeout=self.config.request_timeout_seconds,
            headers={
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "cs,en;q=0.7",
            },
        )
        return parse_detail_description(detail)

    def collect_feedback(self, telegram: TelegramClient | None = None) -> int:
        if not self.config.telegram.bot_token:
            return 0
        telegram = telegram or self._telegram()
        offset = int(self.storage.get_metadata("telegram_update_offset", "0") or 0)
        if not hasattr(telegram, "get_feedback_updates"):
            feedback, next_offset = telegram.poll_feedback(offset)
            for item in feedback:
                self.storage.record_feedback(**item)
        else:
            feedback, next_offset = telegram.get_feedback_updates(offset)
            for item in feedback:
                persisted = self.storage.record_feedback(
                    source=item["source"],
                    external_id=item["external_id"],
                    label=item["label"],
                    user=item.get("user", ""),
                    callback_query_id=item.get("callback_query_id", ""),
                    telegram_message_id=item.get("telegram_message_id"),
                    telegram_chat_id=item.get("telegram_chat_id", ""),
                )
                listing = self.storage.get_listing(item["source"], item["external_id"])
                try:
                    telegram.apply_feedback_action(item, listing, repeated=not persisted)
                except Exception:
                    LOGGER.exception(
                        "Telegram feedback action failed for %s:%s",
                        item["source"],
                        item["external_id"],
                    )
        if next_offset != offset:
            self.storage.set_metadata("telegram_update_offset", str(next_offset))
        return len(feedback)

    def process_once(self, telegram: TelegramClient | None = None) -> dict[str, int]:
        listings = self.fetch_all()
        first_run = self.storage.is_empty()
        suppress_keys: set[str] = set()
        if first_run:
            if self.config.bootstrap_mode == "skip_existing":
                suppress_keys = {listing.key for listing in listings}
            elif self.config.bootstrap_mode == "send_latest":
                allowed = {listing.key for listing in listings[: self.config.max_initial_notifications]}
                suppress_keys = {listing.key for listing in listings if listing.key not in allowed}
        new_listings = self.storage.register_new(listings, suppress_keys)
        inserted = len(new_listings)
        duplicate_stats = self.storage.backfill_duplicates(
            self.config.priority,
            description_loader=self._duplicate_detail_description,
        )
        self.storage.refresh_duplicate_used_comparables(
            self.config.priority.used_comparable_max_age_days
        )
        duplicate_suppressed_keys = {
            listing.key
            for listing in new_listings
            if (
                (record := self.storage.get_duplicate_record(listing)) is not None
                and not bool(record["is_canonical"])
                and record["match_level"] == "confirmed"
            )
        }

        telegram = telegram or self._telegram()
        feedback_count = self.collect_feedback(telegram)
        finder = self._retail_finder()
        market_finder = self._market_finder()
        analyzed: dict[str, tuple[Listing, ListingAnalysis]] = {}
        cache_hits = 0
        market_cache_hits = 0
        for listing in new_listings:
            identity = finder.identify(listing) if finder else identify_listing(listing)
            used = self.storage.find_used_comparables(
                listing,
                max_age_days=self.config.priority.used_comparable_max_age_days,
            )
            valuation = None
            market_valuation: MarketValuation | None = None
            cache_used = False
            if finder and identity.brand and identity.model:
                cached = self.storage.get_cached_valuation(valuation_cache_key(identity))
                if cached is not None:
                    valuation = finder.as_cached(cached)
                    cache_used = True
                    cache_hits += 1
            if market_finder and identity.brand and identity.model:
                market_valuation = self.storage.get_cached_market_valuation(listing)
                if market_valuation is not None:
                    used = market_valuation.as_used_comparables()
                    market_cache_hits += 1
            analysis = build_analysis(
                listing,
                self.config.priority,
                identity=identity,
                valuation=valuation,
                used_comparables=used,
                cache_used=cache_used and (market_valuation is not None or market_finder is None),
            )
            analysis.market_valuation = market_valuation
            analysis.duplicate_alternatives = self.storage.duplicate_alternatives(listing)
            if listing.key in suppress_keys:
                analysis.notification_status = "not_selected"
                analysis.notification_reason = "bootstrap_suppressed"
            elif listing.key in duplicate_suppressed_keys:
                analysis.notification_status = "not_selected"
                analysis.notification_reason = "confirmed_cross_source_duplicate"
            self.storage.save_analysis(listing, analysis)
            analyzed[listing.key] = (listing, analysis)

        lookup_budget = (
            dynamic_lookup_budget(len(new_listings), self.config.priority)
            if finder or market_finder
            else 0
        )
        lookup_pool = [
            value for key, value in analyzed.items()
            if key not in suppress_keys and key not in duplicate_suppressed_keys
        ]
        lookups = select_lookup_candidates(lookup_pool, lookup_budget)
        lookup_count = 0
        consecutive_errors = 0
        market_http_requests: dict[str, int] = {}
        for index, (listing, previous) in enumerate(lookups):
            if (
                finder
                and market_finder is None
                and consecutive_errors >= self.config.retail.max_consecutive_source_errors
            ):
                LOGGER.warning("Price lookups stopped after %d consecutive errors", consecutive_errors)
                break
            if index and self.config.retail.lookup_delay_seconds > 0:
                time.sleep(self.config.retail.lookup_delay_seconds)
            identity = previous.identity
            if identity is None:
                continue
            lookup_count += 1
            valuation = previous.valuation
            if finder and (valuation is None or not previous.cache_used):
                try:
                    valuation = finder.find(listing, identity)
                    self.storage.cache_valuation_hours(
                        valuation_cache_key(identity),
                        valuation,
                        finder.cache_ttl_hours(valuation.status),
                    )
                    consecutive_errors = 0
                except Exception as exc:
                    consecutive_errors += 1
                    previous.risks.append(f"Поиск новой цены завершился ошибкой {type(exc).__name__}.")
                    LOGGER.exception("Retail valuation failed for %s; used-market search continues", listing.key)

            market_valuation = previous.market_valuation
            if market_finder and market_valuation is None:
                try:
                    market_valuation = market_finder.find(
                        listing,
                        identity,
                        new_valuation=valuation,
                    )
                    for source, count in market_valuation.http_requests.items():
                        market_http_requests[source] = market_http_requests.get(source, 0) + count
                except Exception as exc:
                    previous.risks.append(f"Рыночная оценка завершилась ошибкой {type(exc).__name__}.")
                    LOGGER.exception("Market valuation failed for %s; listing remains eligible", listing.key)

            used = (
                market_valuation.as_used_comparables()
                if market_valuation is not None
                else previous.used_comparables
            )
            analysis = build_analysis(
                listing,
                self.config.priority,
                identity=identity,
                valuation=valuation,
                used_comparables=used,
                cache_used=False,
            )
            analysis.market_valuation = market_valuation
            analysis.duplicate_alternatives = previous.duplicate_alternatives
            if consecutive_errors and market_valuation is None:
                analysis.preliminary_priority_score = max(
                    analysis.preliminary_priority_score,
                    self.config.priority.manual_review_min_score,
                )
                analysis.priority_class = "manual_review"
            analyzed[listing.key] = (listing, analysis)
            self.storage.save_analysis(listing, analysis)

        if self.config.deal_scoring.enabled:
            evaluator = DealEvaluator(self.config.deal_scoring)
            for listing, analysis in analyzed.values():
                try:
                    deal_evaluation = evaluator.evaluate(
                        listing,
                        analysis,
                        self.storage.get_deal_costs(listing),
                    )
                except Exception as exc:
                    LOGGER.exception("Deal evaluation failed for %s; cycle continues", listing.key)
                    deal_evaluation = evaluator.error_result(listing, exc)
                analysis.deal_evaluation = deal_evaluation
                self.storage.save_deal_evaluation(deal_evaluation)
                self.storage.save_analysis(listing, analysis)

        now = datetime.now(UTC)
        notification_pool: list[tuple[Listing, ListingAnalysis, float]] = []
        for key, (listing, analysis) in analyzed.items():
            if key in suppress_keys:
                continue
            if key in duplicate_suppressed_keys:
                continue
            first_seen = self.storage.first_seen_at(listing)
            age_hours = max(0.0, (now - first_seen.astimezone(UTC)).total_seconds() / 3600)
            notification_pool.append((listing, analysis, age_hours))
        if self.config.deal_scoring.enabled:
            selected = select_deal_notifications(
                notification_pool,
                self.config.deal_scoring,
                max_cards=self.config.priority.max_telegram_cards,
                manual_review_reserved_slots=self.config.priority.manual_review_reserved_slots,
                max_age_hours=self.config.priority.individual_notification_max_age_hours,
            )
        else:
            selected = select_notifications(notification_pool, self.config.priority)
        selected_keys = {listing.key for listing, _ in selected}

        sent = 0
        for listing, analysis in selected:
            try:
                message_id = telegram.send_listing(
                    listing,
                    retail_enabled=False,
                    analysis=analysis,
                )
                self.storage.mark_sent(listing, message_id)
                analysis.notification_status = "sent"
                analysis.notification_reason = ""
                self.storage.save_analysis(listing, analysis)
                sent += 1
                valuation = analysis.valuation
                LOGGER.info(
                    "Selected %s score=%d class=%s deal_status=%s deal_score=%s "
                    "confidence=%s cache=%s sources=%d reasons=%s",
                    listing.key,
                    analysis.preliminary_priority_score,
                    analysis.priority_class,
                    analysis.deal_evaluation.status if analysis.deal_evaluation else "disabled",
                    analysis.deal_evaluation.deal_score if analysis.deal_evaluation else "n/a",
                    analysis.analysis_confidence,
                    analysis.cache_used,
                    valuation.independent_source_count if valuation else 0,
                    "; ".join(analysis.reasons[:4]),
                )
            except Exception as exc:
                analysis.notification_status = "analysis_failed"
                analysis.notification_reason = f"telegram_{type(exc).__name__}"
                self.storage.save_analysis(listing, analysis)
                LOGGER.exception("Telegram delivery failed for %s", listing.key)

        expired = 0
        for listing, analysis, age_hours in notification_pool:
            if listing.key in selected_keys:
                continue
            if analysis.priority_class == "excluded":
                status, reason = "excluded", analysis.notification_reason or "hard_filter"
            elif age_hours > self.config.priority.individual_notification_max_age_hours:
                status, reason = "expired", "individual_notification_too_old"
                expired += 1
            elif self.config.deal_scoring.enabled and analysis.deal_evaluation is not None:
                deal_status = analysis.deal_evaluation.status
                if deal_status == "LOW_PRIORITY":
                    status, reason = "low_priority", "deal_status_low_priority_not_sent"
                elif deal_status == "REJECT":
                    status, reason = "excluded", "deal_status_reject_not_sent"
                else:
                    status, reason = "not_selected", "deal_notification_slots_or_policy"
            elif analysis.priority_class == "low_priority":
                status, reason = "low_priority", "score_below_manual_review_threshold"
            else:
                status, reason = "not_selected", "notification_slots_exhausted"
            analysis.notification_status = status
            analysis.notification_reason = reason
            self.storage.save_analysis(listing, analysis)

        final_analyses = [analysis for _, analysis in analyzed.values()]
        one_source = sum(bool(item.valuation and item.valuation.independent_source_count == 1) for item in final_analyses)
        two_sources = sum(bool(item.valuation and item.valuation.independent_source_count == 2) for item in final_analyses)
        three_plus = sum(bool(item.valuation and item.valuation.independent_source_count >= 3) for item in final_analyses)
        market_values = [item.market_valuation for item in final_analyses if item.market_valuation]
        deal_values = [item.deal_evaluation for item in final_analyses if item.deal_evaluation]
        stats = {
            "fetched": len(listings),
            "new": inserted,
            "hard_excluded": sum(item.priority_class == "excluded" for item in final_analyses),
            "scored": len(final_analyses),
            "cache_hits": cache_hits,
            "market_cache_hits": market_cache_hits + sum(item.cache_hits for item in market_values),
            "market_cache_misses": sum(item.cache_misses for item in market_values),
            "price_lookups": lookup_count,
            "one_source": one_source,
            "two_sources": two_sources,
            "three_plus_sources": three_plus,
            "price_not_found": sum(not item.valuation or item.valuation.median_price_czk is None for item in final_analyses),
            "urgent": sum(item.priority_class == "urgent_candidate" for item in final_analyses),
            "interesting": sum(item.priority_class == "interesting_candidate" for item in final_analyses),
            "manual_review": sum(item.priority_class == "manual_review" for item in final_analyses),
            "low_priority": sum(item.priority_class == "low_priority" for item in final_analyses),
            "sent": sent,
            "saved_without_send": inserted - sent,
            "expired": expired,
            "feedback": feedback_count,
            "duplicate_groups": duplicate_stats["groups"],
            "confirmed_duplicate_suppressed": len(duplicate_suppressed_keys),
            "market_evaluated": sum(item.market_price_czk is not None for item in market_values),
            "market_high": sum(item.confidence == "high" for item in market_values),
            "market_medium": sum(item.confidence == "medium" for item in market_values),
            "market_low": sum(item.confidence == "low" for item in market_values),
            "market_cz_only": sum(item.countries_used == ["CZ"] for item in market_values),
            "market_foreign": sum(any(country != "CZ" for country in item.countries_used) for item in market_values),
            "market_duplicates_removed": sum(item.duplicates_removed for item in market_values),
            "deal_evaluated": len(deal_values),
            "deal_hot": sum(item.status == "HOT" for item in deal_values),
            "deal_interesting": sum(item.status == "INTERESTING" for item in deal_values),
            "deal_manual_review": sum(item.status == "MANUAL_REVIEW" for item in deal_values),
            "deal_low_priority": sum(item.status == "LOW_PRIORITY" for item in deal_values),
            "deal_reject": sum(item.status == "REJECT" for item in deal_values),
        }
        for source, count in market_http_requests.items():
            stats[f"market_http_{source}"] = count
        LOGGER.info("Stage 2.2 cycle funnel: %s", stats)
        return stats

    def run_forever(self) -> None:
        LOGGER.info(
            "Deal Radar started; marketplaces every %d seconds, Telegram feedback every %d seconds",
            self.config.poll_interval_seconds,
            self.config.feedback_poll_interval_seconds,
        )
        telegram = self._telegram()
        next_marketplace_poll = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= next_marketplace_poll:
                started = now
                try:
                    stats = self.process_once(telegram)
                    LOGGER.info("Marketplace cycle complete: %s", stats)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    LOGGER.exception("Marketplace cycle failed")
                next_marketplace_poll = started + self._next_interval()
                continue

            try:
                feedback_count = self.collect_feedback(telegram)
                if feedback_count:
                    LOGGER.info("Saved %d Telegram feedback actions", feedback_count)
            except KeyboardInterrupt:
                raise
            except Exception:
                LOGGER.exception("Telegram feedback poll failed")
            seconds_until_marketplace_poll = max(0.0, next_marketplace_poll - time.monotonic())
            time.sleep(
                max(
                    0.2,
                    min(self.config.feedback_poll_interval_seconds, seconds_until_marketplace_poll),
                )
            )
