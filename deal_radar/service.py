from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from datetime import UTC, datetime

from deal_radar.config import AppConfig
from deal_radar.bike_identity import identify_listing
from deal_radar.models import Listing, ListingAnalysis
from deal_radar.price_sources import PriceSource, ZboziPriceSource
from deal_radar.pricing import NewBikePriceService
from deal_radar.pricing import valuation_cache_key
from deal_radar.priority import build_analysis, dynamic_lookup_budget, select_lookup_candidates, select_notifications
from deal_radar.sources.bazos import BazosSource
from deal_radar.sources.cyklobazar import CyklobazarSource
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

    def close(self) -> None:
        self.storage.close()

    def fetch_all(self) -> list[Listing]:
        results: list[Listing] = []
        errors: list[str] = []
        successful_sources = 0
        for source in self.sources:
            label = getattr(source, "label", None) or getattr(source.profile, "name", "marketplace")
            try:
                fetched = source.fetch()
                successful_sources += 1
                LOGGER.info("Profile %s: %d matching listings", label, len(fetched))
                results.extend(fetched)
            except Exception as exc:  # keep other profiles alive
                LOGGER.exception("Profile %s failed", label)
                errors.append(f"{label}: {exc}")
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

        telegram = telegram or self._telegram()
        feedback_count = self.collect_feedback(telegram)
        finder = self._retail_finder()
        analyzed: dict[str, tuple[Listing, ListingAnalysis]] = {}
        cache_hits = 0
        for listing in new_listings:
            identity = finder.identify(listing) if finder else identify_listing(listing)
            used = self.storage.find_used_comparables(
                listing,
                max_age_days=self.config.priority.used_comparable_max_age_days,
            )
            valuation = None
            cache_used = False
            if finder and identity.brand and identity.model:
                cached = self.storage.get_cached_valuation(valuation_cache_key(identity))
                if cached is not None:
                    valuation = finder.as_cached(cached)
                    cache_used = True
                    cache_hits += 1
            analysis = build_analysis(
                listing,
                self.config.priority,
                identity=identity,
                valuation=valuation,
                used_comparables=used,
                cache_used=cache_used,
            )
            if listing.key in suppress_keys:
                analysis.notification_status = "not_selected"
                analysis.notification_reason = "bootstrap_suppressed"
            self.storage.save_analysis(listing, analysis)
            analyzed[listing.key] = (listing, analysis)

        lookup_budget = dynamic_lookup_budget(len(new_listings), self.config.priority) if finder else 0
        lookup_pool = [
            value for key, value in analyzed.items()
            if key not in suppress_keys
        ]
        lookups = select_lookup_candidates(lookup_pool, lookup_budget)
        lookup_count = 0
        consecutive_errors = 0
        for index, (listing, previous) in enumerate(lookups):
            if consecutive_errors >= self.config.retail.max_consecutive_source_errors:
                LOGGER.warning("Price lookups stopped after %d consecutive errors", consecutive_errors)
                break
            if index and self.config.retail.lookup_delay_seconds > 0:
                time.sleep(self.config.retail.lookup_delay_seconds)
            identity = previous.identity
            if identity is None:
                continue
            lookup_count += 1
            try:
                valuation = finder.find(listing, identity)
                self.storage.cache_valuation_hours(
                    valuation_cache_key(identity),
                    valuation,
                    finder.cache_ttl_hours(valuation.status),
                )
                consecutive_errors = 0
                analysis = build_analysis(
                    listing,
                    self.config.priority,
                    identity=identity,
                    valuation=valuation,
                    used_comparables=previous.used_comparables,
                    cache_used=False,
                )
                analyzed[listing.key] = (listing, analysis)
                self.storage.save_analysis(listing, analysis)
            except Exception as exc:
                consecutive_errors += 1
                previous.risks.append(f"Поиск новой цены завершился ошибкой {type(exc).__name__}.")
                previous.preliminary_priority_score = max(
                    previous.preliminary_priority_score,
                    self.config.priority.manual_review_min_score,
                )
                previous.priority_class = "manual_review"
                self.storage.save_analysis(listing, previous)
                LOGGER.exception("Retail valuation failed for %s; listing remains eligible", listing.key)

        now = datetime.now(UTC)
        notification_pool: list[tuple[Listing, ListingAnalysis, float]] = []
        for key, (listing, analysis) in analyzed.items():
            if key in suppress_keys:
                continue
            first_seen = self.storage.first_seen_at(listing)
            age_hours = max(0.0, (now - first_seen.astimezone(UTC)).total_seconds() / 3600)
            notification_pool.append((listing, analysis, age_hours))
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
                    "Selected %s score=%d class=%s confidence=%s cache=%s sources=%d reasons=%s",
                    listing.key,
                    analysis.preliminary_priority_score,
                    analysis.priority_class,
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
        stats = {
            "fetched": len(listings),
            "new": inserted,
            "hard_excluded": sum(item.priority_class == "excluded" for item in final_analyses),
            "scored": len(final_analyses),
            "cache_hits": cache_hits,
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
        }
        LOGGER.info("Stage 1.2 cycle funnel: %s", stats)
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
                next_marketplace_poll = started + self.config.poll_interval_seconds
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
