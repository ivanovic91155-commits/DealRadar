from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from datetime import datetime

from deal_radar.config import AppConfig
from deal_radar.codex_fallback import CodexMatchResolver
from deal_radar.models import Listing
from deal_radar.price_sources import PriceSource, ZboziPriceSource
from deal_radar.pricing import NewBikePriceService
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
        self.sources = [BazosSource(profile, config.request_timeout_seconds) for profile in config.profiles]
        self.sources.extend(
            CyklobazarSource(profile, config.request_timeout_seconds)
            for profile in config.cyklobazar_profiles
            if profile.enabled
        )
        self.storage = Storage(config.database_path)

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
        resolver = None
        if self.config.retail.codex_enabled:
            resolver = CodexMatchResolver(
                executable=self.config.retail.codex_path,
                schema_path=self.config.retail.codex_schema_path,
                timeout_seconds=self.config.retail.codex_timeout_seconds,
                calls_per_hour=self.config.retail.codex_calls_per_hour,
                calls_per_day=self.config.retail.codex_calls_per_day,
            )
        return NewBikePriceService(self.config.retail, sources, resolver)

    def collect_feedback(self, telegram: TelegramClient | None = None) -> int:
        if not self.config.telegram.bot_token:
            return 0
        telegram = telegram or self._telegram()
        offset = int(self.storage.get_metadata("telegram_update_offset", "0") or 0)
        feedback, next_offset = telegram.poll_feedback(offset)
        for item in feedback:
            self.storage.record_feedback(**item)
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
        inserted = self.storage.register(listings, suppress_keys)

        telegram = telegram or self._telegram()
        feedback_count = self.collect_feedback(telegram)
        pending = self.storage.pending(self.config.max_notifications_per_run)
        finder = self._retail_finder()
        enrichments_left = self.config.retail.max_enrichments_per_run
        sent = 0
        enriched = 0
        for listing in pending:
            message_id = telegram.send_listing(listing, retail_enabled=finder is not None)
            self.storage.mark_sent(listing, message_id)
            sent += 1
            LOGGER.info("Sent %s", listing.key)
            if finder is None or enrichments_left <= 0:
                continue
            enrichments_left -= 1
            identity = finder.identify(listing)
            cache_key = identity.normalized_key
            valuation = self.storage.get_cached_valuation(cache_key)
            try:
                if valuation is None:
                    valuation = finder.find(listing, identity)
                    self.storage.cache_valuation_hours(
                        cache_key,
                        valuation,
                        finder.cache_ttl_hours(valuation.status),
                    )
                else:
                    valuation = finder.as_cached(valuation)
                telegram.send_valuation(
                    listing,
                    valuation,
                    message_id,
                    max_sources=self.config.retail.max_telegram_sources,
                )
                enriched += 1
            except Exception:
                LOGGER.exception("Retail valuation failed for %s; urgent listing alert was still delivered", listing.key)
        return {"fetched": len(listings), "inserted": inserted, "sent": sent, "enriched": enriched, "feedback": feedback_count}

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
