from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterable
from datetime import datetime

from deal_radar.config import AppConfig
from deal_radar.codex_fallback import CodexMatchResolver
from deal_radar.deal_engine import DealVerdict, evaluate_deal
from deal_radar.bike_identity import identify_listing
from deal_radar.models import Listing, Valuation
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
        # Backoff по источникам: label -> (paused_until_monotonic, current_delay).
        # При 403/429 источник ставится на паузу с экспоненциальным ростом.
        self._source_backoff: dict[str, tuple[float, float]] = {}

    def close(self) -> None:
        self.storage.close()

    @staticmethod
    def _is_rate_limited(exc: Exception) -> bool:
        text = str(exc)
        return "403" in text or "429" in text

    def fetch_all(self) -> list[Listing]:
        results: list[Listing] = []
        errors: list[str] = []
        successful_sources = 0
        active_sources = 0
        now = time.monotonic()
        for source in self.sources:
            label = getattr(source, "label", None) or getattr(source.profile, "name", "marketplace")
            paused = self._source_backoff.get(label)
            if paused and now < paused[0]:
                LOGGER.info("Profile %s on backoff, skipping this cycle", label)
                continue
            active_sources += 1
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

    def _evaluate_listing(
        self,
        listing: Listing,
        finder: NewBikePriceService | None,
        enrich: bool,
    ) -> tuple[DealVerdict, Valuation | None]:
        """Идентификация + (опционально) поиск цены нового + скоринг.

        Если enrich=False, цена нового не ищется (экономим запросы к источникам),
        маржа при этом опирается только на кэш, если он есть.
        """
        identity = finder.identify(listing) if finder is not None else identify_listing(listing)
        # Копим историю реальных цен б/у по модели — будущая замена амортизации.
        self.storage.record_price_observation(
            listing,
            model_key=identity.normalized_key,
            brand=identity.brand,
            model=identity.model,
            model_year=identity.model_year,
        )
        valuation: Valuation | None = None
        if finder is not None:
            cache_key = identity.normalized_key
            valuation = self.storage.get_cached_valuation(cache_key)
            if valuation is None and enrich:
                try:
                    valuation = finder.find(listing, identity)
                    self.storage.cache_valuation_hours(
                        cache_key, valuation, finder.cache_ttl_hours(valuation.status)
                    )
                except Exception:
                    LOGGER.exception("Retail valuation failed for %s", listing.key)
                    valuation = None
            elif valuation is not None:
                valuation = finder.as_cached(valuation)
        verdict = evaluate_deal(listing, identity, valuation, self.config.scoring)
        return verdict, valuation

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

        # Этап 1: предварительный скоринг без дорогого поиска цены (только кэш).
        # Нужен, чтобы потратить лимит обогащений на самых перспективных.
        prelim: list[tuple[Listing, DealVerdict, Valuation | None]] = []
        for listing in pending:
            verdict, valuation = self._evaluate_listing(listing, finder, enrich=False)
            prelim.append((listing, verdict, valuation))
        # Кандидаты без цены (маржа=0) — вверх по остальным компонентам:
        # у них выше шанс стать сделкой после уточнения цены.
        prelim.sort(key=lambda item: item[1].score, reverse=True)

        # Этап 2: для верхних без готовой цены — ищем цену нового и пересчитываем.
        enrich_left = self.config.retail.max_enrichments_per_run
        scored: list[tuple[Listing, DealVerdict, Valuation | None]] = []
        for listing, verdict, valuation in prelim:
            if finder is not None and valuation is None and enrich_left > 0:
                verdict, valuation = self._evaluate_listing(listing, finder, enrich=True)
                enrich_left -= 1
            scored.append((listing, verdict, valuation))

        # Этап 3: маршрутизация по тиру.
        scored.sort(key=lambda item: item[1].score, reverse=True)
        sent = 0
        hot = 0
        archived = 0
        for listing, verdict, valuation in scored:
            if not verdict.should_notify:
                # archive. Если цену нового так и не уточнили (маржа не считалась
                # из-за лимита обогащений) — НЕ помечаем sent_at, чтобы объявление
                # вернулось в очередь и получило шанс на оценку в следующем цикле.
                priced = valuation is not None
                self.storage.mark_scored(
                    listing, verdict, message_id=None, keep_pending=not priced
                )
                archived += 1
                LOGGER.info("Archived %s score=%d tier=%s", listing.key, verdict.score, verdict.tier)
                continue
            message_id = telegram.send_listing(
                listing, retail_enabled=finder is not None, verdict=verdict
            )
            self.storage.mark_scored(listing, verdict, message_id=message_id)
            sent += 1
            if verdict.is_hot:
                hot += 1
            LOGGER.info("Sent %s score=%d tier=%s", listing.key, verdict.score, verdict.tier)
            if valuation is not None and valuation.median_price_czk is not None:
                try:
                    telegram.send_valuation(
                        listing, valuation, message_id,
                        max_sources=self.config.retail.max_telegram_sources,
                    )
                except Exception:
                    LOGGER.exception("Valuation message failed for %s", listing.key)
        return {
            "fetched": len(listings), "inserted": inserted, "sent": sent,
            "hot": hot, "archived": archived, "feedback": feedback_count,
        }

    def _next_interval(self) -> float:
        """Интервал до следующего опроса с небольшим случайным джиттером,
        чтобы запросы не били строго по расписанию (естественнее для площадки)."""
        base = self.config.poll_interval_seconds
        jitter = self.config.poll_jitter_seconds
        if jitter <= 0:
            return float(base)
        return base + random.uniform(-jitter, jitter)

    def run_forever(self) -> None:
        LOGGER.info(
            "Deal Radar started; marketplaces ~every %d s (±%d jitter), feedback every %d s",
            self.config.poll_interval_seconds,
            self.config.poll_jitter_seconds,
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
