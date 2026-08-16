from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime

from deal_radar.ai.budget import BudgetGuard
from deal_radar.ai.listing_analysis import ListingAnalyzer, content_fingerprint
from deal_radar.ai.prefilter import REASON_BUDGET, ai_prefilter
from deal_radar.ai.price_estimate import (
    PriceEstimator,
    estimate_key,
    needs_estimate,
    to_market_valuation,
)
from deal_radar.ai_gate import (
    ACTION_BLOCKED,
    ACTION_DEEP_ANALYSIS,
    BLOCK,
    NON_BIKE_LISTING_TYPES,
    SEND,
    analysis_priority,
    read_signals,
    telegram_decision,
)
from deal_radar.config import AppConfig
from deal_radar.bike_identity import configure_identity, identify_listing
from deal_radar.deal_scoring import DealEvaluator, select_deal_notifications
from deal_radar.exchange_rates import ExchangeRateProvider
from deal_radar.http import HttpError, get_bytes
from deal_radar.market_pricing import MarketPriceEngine
from deal_radar.market_sources import (
    BazosCzechMarketSource,
    BuycycleMarketSource,
    KleinanzeigenMarketSource,
    MarktplaatsMarketSource,
    UsedMarketSource,
)
from deal_radar.models import AIPriceEstimate, Listing, ListingAnalysis, MarketValuation
from deal_radar.price_sources import PriceSource, ZboziPriceSource
from deal_radar.pricing import NewBikePriceService
from deal_radar.pricing import valuation_cache_key
from deal_radar.priority import build_analysis, dynamic_lookup_budget, select_lookup_candidates, select_notifications
from deal_radar.sources.bazos import BazosSource
from deal_radar.sources.cyklobazar import CyklobazarSource, parse_detail_description
from deal_radar.sources.facebook import FacebookMarketplaceSource
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
        configure_identity(config.identity)
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
        self.sources.extend(self._facebook_sources())
        # Backoff по источникам объявлений: label -> (paused_until_monotonic, delay).
        # При 403/429 источник ставится на паузу с экспоненциальным ростом, чтобы
        # не долбить площадку и не спровоцировать блокировку.
        self._source_backoff: dict[str, tuple[float, float]] = {}
        self._ai_analyzer_cache: ListingAnalyzer | None = None
        self._ai_setup_logged = False
        self._price_estimator_cache: PriceEstimator | None = None
        self._price_setup_logged = False

    def _facebook_sources(self) -> list[FacebookMarketplaceSource]:
        """Профили Facebook Marketplace, если источник включён и настроен.

        Отсутствие ключа выключает ровно этот источник: Bazoš и Cyklobazar
        продолжают работать, а причина называется один раз при старте.
        """

        config = self.config.facebook_marketplace
        if not config.enabled:
            return []
        if not config.api_key:
            LOGGER.error(
                "Facebook Marketplace is enabled but SCRAPECREATORS_API_KEY is empty; "
                "set it in the deployment environment or turn FACEBOOK_MARKETPLACE_ENABLED off"
            )
            return []
        profiles = config.active_profiles
        if not profiles:
            LOGGER.warning("Facebook Marketplace is enabled but no active profile is configured")
            return []
        LOGGER.info(
            "Facebook Marketplace enabled: %d profile(s), up to %d credits per cycle, ~%d per month",
            len(profiles),
            len(profiles) * config.max_pages_per_profile,
            config.credits_per_month(),
        )
        return [FacebookMarketplaceSource(profile, config) for profile in profiles]

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

    def _ai_analyzer(self) -> ListingAnalyzer | None:
        """Анализатор Level 1 или ``None``, если AI выключен либо не настроен.

        Отсутствие ключа — не повод останавливать парсер (раздел 15 ТЗ):
        сообщаем один раз, называя переменную окружения, и работаем дальше.
        """

        config = self.config.ai
        if not config.enabled:
            return None
        if not config.api_key:
            if not self._ai_setup_logged:
                LOGGER.error(
                    "AI analysis is enabled but OPENAI_API_KEY is empty; "
                    "set it in the deployment environment or turn AI_ANALYSIS_ENABLED off"
                )
                self._ai_setup_logged = True
            return None
        if self._ai_analyzer_cache is None:
            try:
                self._ai_analyzer_cache = ListingAnalyzer(config)
            except Exception:
                if not self._ai_setup_logged:
                    LOGGER.exception("AI analyzer could not be built; the AI stage stays disabled")
                    self._ai_setup_logged = True
                return None
        return self._ai_analyzer_cache

    def _run_ai_analysis(
        self,
        analyzed: dict[str, tuple[Listing, ListingAnalysis]],
        skip_keys: set[str],
    ) -> dict[str, int]:
        """Фаза AI Level 1: анализ каждого нового объявления после pre-filter.

        Вызовы уходят в пул потоков, но всё общение с SQLite остаётся здесь, на
        главном потоке, поэтому соединение ``Storage`` не расшаривается.
        """

        analyzer = self._ai_analyzer()
        if analyzer is None:
            # Пустой словарь, а не нули: при выключенном AI funnel-лог обязан
            # остаться в точности таким же, каким был до этого этапа.
            return {}
        stats = {
            "ai_calls": 0,
            "ai_cache_hits": 0,
            "ai_skipped": 0,
            "ai_failed": 0,
            "ai_pending": 0,
            "ai_cost_usd": 0.0,
        }
        config = self.config.ai
        guard = BudgetGuard(config, self.storage)
        guard.check()  # предупреждение о бюджете пишется один раз за цикл

        pending: list[tuple[Listing, ListingAnalysis]] = []
        for key, (listing, analysis) in analyzed.items():
            prefilter = ai_prefilter(
                listing,
                config,
                identity=analysis.identity,
                is_duplicate=key in skip_keys,
            )
            if not prefilter.passed:
                analysis.ai_analysis = analyzer.skipped(listing, prefilter)
                stats["ai_skipped"] += 1
                continue
            cached = self.storage.get_cached_ai_analysis(
                content_hash=content_fingerprint(listing),
                prompt_name=analyzer.prompt.name,
                prompt_version=analyzer.prompt.version,
                schema_version=analyzer.prompt.schema_version,
                model_name=config.model_primary,
            )
            if cached is not None:
                cached.cache_used = True
                analysis.ai_analysis = cached
                stats["ai_cache_hits"] += 1
                continue
            pending.append((listing, analysis))

        limit = config.max_calls_per_cycle
        if limit > 0 and len(pending) > limit:
            for listing, analysis in pending[limit:]:
                analysis.ai_analysis = analyzer.pending(listing, "CYCLE_LIMIT")
                stats["ai_pending"] += 1
            pending = pending[:limit]

        # Половина интервала опроса — потолок для всей фазы, чтобы зависший API
        # не съел цикл целиком.
        deadline = time.monotonic() + max(30.0, self.config.poll_interval_seconds / 2)
        chunk_size = max(1, config.max_parallel_requests) * 5
        for start in range(0, len(pending), chunk_size):
            chunk = pending[start : start + chunk_size]
            # Бюджет перепроверяется между пачками: за время предыдущей он мог
            # исчерпаться, и остаток объявлений должен остаться AI_PENDING.
            budget = guard.state()
            if budget.stopped or time.monotonic() >= deadline:
                reason = REASON_BUDGET if budget.stopped else "CYCLE_DEADLINE"
                for listing, analysis in pending[start:]:
                    analysis.ai_analysis = analyzer.pending(listing, reason)
                    stats["ai_pending"] += 1
                break
            self._analyze_chunk(analyzer, chunk, stats)

        # Единственное место, где результат фазы попадает в базу: один UPDATE на
        # объявление вместо записи в каждой ветке выше.
        for listing, analysis in analyzed.values():
            if analysis.ai_analysis is not None:
                self.storage.save_analysis(listing, analysis)
        return stats

    # Типы объявлений, которые AI считает «не велосипедом» и которые уместно
    # тихо не слать. Один список на оба замка: подавление здесь и блокирующий
    # риск в AI Gate.
    _NON_BIKE_LISTING_TYPES = NON_BIKE_LISTING_TYPES

    def _ai_suppressed_keys(
        self, analyzed: dict[str, tuple[Listing, ListingAnalysis]]
    ) -> set[str]:
        """Объявления, которые AI уверенно опознал как не-велосипед.

        Единственный случай, когда классификации Level 1 разрешено убрать
        карточку. Строго в одну сторону — только скрыть очевидный не-велосипед,
        никогда не поднять статус и не тронуть цену. Предохранители: слой должен
        быть выведен из тени, уверенность — не ниже порога, тип объявления — из
        списка не-велосипедов.

        ``can_affect_deal_status`` здесь намеренно не проверяется: этот флаг
        разрешает AI влиять на статус сделки и на цену, а «это шлем, а не
        велосипед» — вопрос релевантности. Из-за лишнего условия шлемы и детские
        кресла уходили в Telegram у всех, кто оставил флаг статусов выключенным.
        """

        config = self.config.ai
        candidates: set[str] = set()
        for key, (_listing, analysis) in analyzed.items():
            ai = analysis.ai_analysis
            if ai is None or ai.status != "AI_OK" or ai.classification is None:
                continue
            classification = ai.classification
            if classification.is_bicycle:
                continue
            if classification.listing_type not in self._NON_BIKE_LISTING_TYPES:
                continue
            if classification.relevance_confidence < config.min_reject_confidence:
                continue
            candidates.add(key)
        if candidates and not (config.enabled and not config.shadow_mode):
            # Молчать здесь нельзя: снаружи это выглядит как «AI смотрит на фото,
            # но мусор всё равно приходит», а причина — один флаг окружения.
            LOGGER.warning(
                "AI marked %d listing(s) as not a bicycle, but the layer is in shadow mode "
                "and cannot hide them; set AI_SHADOW_MODE=false to apply the verdict",
                len(candidates),
            )
            return set()
        return candidates

    def _run_ai_gate(
        self, analyzed: dict[str, tuple[Listing, ListingAnalysis]]
    ) -> dict[str, int]:
        """AI Opportunity Gate: превратить разбор Level 1 в приоритет анализа.

        Нового вызова API здесь нет — только уже сохранённый JSON. Ворота никого
        не выбрасывают: они решают, кому раньше достанется платный рыночный
        анализ, и оставляют в базе объяснение своего числа.
        """

        config = self.config.ai_gate
        if not config.enabled:
            return {}
        live = self.config.ai_signals_live
        stats = {"ai_gate_passed": 0, "ai_gate_skipped": 0, "ai_gate_blocked": 0}
        total = 0
        for listing, analysis in analyzed.values():
            signals = read_signals(analysis, config, live=live)
            decision = analysis_priority(signals, config)
            analysis.analysis_priority_score = decision.score
            analysis.analysis_priority_reasons = decision.reasons
            analysis.ai_gate_action = decision.action
            total += decision.score
            if decision.action == ACTION_DEEP_ANALYSIS:
                stats["ai_gate_passed"] += 1
            elif decision.action == ACTION_BLOCKED:
                stats["ai_gate_blocked"] += 1
            else:
                stats["ai_gate_skipped"] += 1
            LOGGER.info(
                "AI_GATE listing=%s score=%d action=%s reasons=%s",
                listing.key,
                decision.score,
                decision.action,
                ",".join(decision.reasons),
            )
            self.storage.save_analysis(listing, analysis)
        if analyzed:
            stats["avg_analysis_priority"] = round(total / len(analyzed), 1)
        return stats

    def _run_telegram_gate(
        self, pool: list[tuple[Listing, ListingAnalysis, float]]
    ) -> dict[str, int]:
        """Telegram Notification Gate: у каждой карточки должна быть причина.

        Обычный ``MANUAL_REVIEW`` больше не уходит человеку только потому, что
        нашёлся свободный слот: нужен сильный сигнал (скрытая возможность,
        спешащий продавец, ценовая аномалия) либо достаточный notification
        score. Решение сохраняется в анализе целиком — и число, и причина.
        """

        config = self.config.telegram_gate
        if not config.enabled:
            # Выключенные ворота не должны влиять и на порядок карточек: без
            # notification_priority_score сортировка остаётся прежней.
            return {}
        stats = {
            "telegram_gate_skipped": 0,
            "telegram_gate_blocked": 0,
            "manual_review_suppressed": 0,
        }
        total = 0
        for listing, analysis, _age_hours in pool:
            signals = read_signals(analysis, self.config.ai_gate, live=self.config.ai_signals_live)
            decision = telegram_decision(analysis, signals, config)
            analysis.notification_priority_score = decision.score
            analysis.notification_reasons = decision.reasons
            analysis.telegram_gate_action = decision.action
            analysis.telegram_gate_reason = decision.reason
            total += decision.score
            status = analysis.deal_evaluation.status if analysis.deal_evaluation else "UNKNOWN"
            if decision.action != SEND:
                stats["telegram_gate_blocked" if decision.action == BLOCK else "telegram_gate_skipped"] += 1
                if status == "MANUAL_REVIEW":
                    stats["manual_review_suppressed"] += 1
            LOGGER.info(
                "TELEGRAM_GATE listing=%s status=%s notification_score=%d action=%s reason=%s",
                listing.key,
                status,
                decision.score,
                decision.action,
                decision.reason,
            )
        if pool:
            stats["avg_notification_priority"] = round(total / len(pool), 1)
        return stats

    def _analyze_chunk(
        self,
        analyzer: ListingAnalyzer,
        chunk: list[tuple[Listing, ListingAnalysis]],
        stats: dict[str, int],
    ) -> None:
        workers = min(self.config.ai.max_parallel_requests, len(chunk))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {
                pool.submit(analyzer.analyze, listing, analysis.identity): (listing, analysis)
                for listing, analysis in chunk
            }
            outcomes = []
            for future, (listing, analysis) in futures.items():
                try:
                    outcomes.append((listing, analysis, future.result()))
                except Exception:
                    LOGGER.exception("AI analysis crashed for %s; listing stays AI_FAILED", listing.key)
                    analysis.ai_analysis = analyzer.pending(listing, "AI_ERROR")
                    analysis.ai_analysis.status = "AI_FAILED"
                    stats["ai_failed"] += 1

        for listing, analysis, outcome in outcomes:
            if outcome.call_log:
                self.storage.log_ai_call(outcome.call_log)
                stats["ai_calls"] += 1
                stats["ai_cost_usd"] = round(
                    stats["ai_cost_usd"] + float(outcome.call_log.get("estimated_total_cost_usd", 0.0)),
                    6,
                )
            analysis.ai_analysis = outcome.analysis
            if outcome.analysis.status == "AI_OK":
                self.storage.cache_ai_analysis(listing, outcome.analysis, self.config.ai.cache_ttl_hours)
            else:
                stats["ai_failed"] += 1

    def _price_estimator(self) -> PriceEstimator | None:
        config = self.config.ai
        if not (config.enabled and config.price_estimate_enabled):
            return None
        if not config.api_key:
            return None  # об отсутствии ключа уже сообщила фаза Level 1
        if self._price_estimator_cache is None:
            try:
                self._price_estimator_cache = PriceEstimator(config)
            except Exception:
                if not self._price_setup_logged:
                    LOGGER.exception("AI price estimator could not be built; the phase stays disabled")
                    self._price_setup_logged = True
                return None
        return self._price_estimator_cache

    def _run_ai_price_estimates(
        self, analyzed: dict[str, tuple[Listing, ListingAnalysis]]
    ) -> dict[str, int]:
        """Фаза AI Level 2: оценка цены там, где аналогов не нашлось.

        Ступени выше — реальные аналоги и амортизация от цены нового — упираются
        в бюджет скрейпинга, поэтому большая часть объявлений приходила сюда
        вообще без цены. Эта фаза HTTP-запросов к площадкам не делает.

        Измеренная цена всегда сильнее оценённой: если движок что-то нашёл,
        его результат остаётся в ``market_valuation``, а ответ модели ложится
        рядом как второе мнение.
        """

        estimator = self._price_estimator()
        if estimator is None:
            return {}
        stats = {
            "ai_price_calls": 0,
            "ai_price_cache_hits": 0,
            "ai_price_applied": 0,
            "ai_price_rejected": 0,
            "ai_price_failed": 0,
            "ai_price_pending": 0,
            "ai_price_cost_usd": 0.0,
        }
        config = self.config.ai
        guard = BudgetGuard(config, self.storage)
        pending: list[tuple[Listing, ListingAnalysis]] = []
        for listing, analysis in analyzed.values():
            if not needs_estimate(analysis.market_valuation, config):
                continue
            key, _kind = estimate_key(listing, analysis.identity, analysis.ai_analysis)
            cached = self.storage.get_cached_ai_price(
                estimate_key=key,
                prompt_name=estimator.prompt.name,
                prompt_version=estimator.prompt.version,
                schema_version=estimator.prompt.schema_version,
                model_name=config.price_model,
            )
            if cached is not None:
                cached.cache_used = True
                self._apply_price(listing, analysis, cached, stats)
                stats["ai_price_cache_hits"] += 1
                continue
            pending.append((listing, analysis))

        # Дедупликация внутри цикла. Кэш в базе спасает только между циклами, а
        # три объявления об одном и том же велосипеде приходят обычно вместе:
        # без этой группировки они ушли бы тремя платными вызовами сразу.
        groups: dict[str, list[tuple[Listing, ListingAnalysis]]] = {}
        for listing, analysis in pending:
            key, _kind = estimate_key(listing, analysis.identity, analysis.ai_analysis)
            groups.setdefault(key, []).append((listing, analysis))
        batches = list(groups.values())

        deadline = time.monotonic() + max(30.0, self.config.poll_interval_seconds / 2)
        chunk_size = max(1, config.max_parallel_requests) * 3
        for start in range(0, len(batches), chunk_size):
            budget = guard.state()
            if budget.stopped or time.monotonic() >= deadline:
                reason = REASON_BUDGET if budget.stopped else "CYCLE_DEADLINE"
                for batch in batches[start:]:
                    for listing, analysis in batch:
                        analysis.ai_price = estimator.pending(
                            listing, analysis.identity, analysis.ai_analysis, reason
                        )
                        stats["ai_price_pending"] += 1
                break
            self._estimate_chunk(estimator, batches[start : start + chunk_size], stats)

        for listing, analysis in analyzed.values():
            if analysis.ai_price is not None:
                self.storage.save_analysis(listing, analysis)
        return stats

    def _estimate_chunk(
        self,
        estimator: PriceEstimator,
        batches: list[list[tuple[Listing, ListingAnalysis]]],
        stats: dict[str, int],
    ) -> None:
        """Одна пачка. Каждый элемент — группа объявлений об одном велосипеде:
        вызов делается по первому из них, результат получает вся группа."""

        workers = min(self.config.ai.max_parallel_requests, len(batches))
        outcomes = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {}
            for batch in batches:
                listing, analysis = batch[0]
                futures[
                    pool.submit(
                        estimator.estimate,
                        listing,
                        analysis.identity,
                        analysis.ai_analysis,
                        analysis.market_valuation,
                        analysis.valuation,
                    )
                ] = batch
            for future, batch in futures.items():
                try:
                    outcomes.append((batch, future.result()))
                except Exception:
                    LOGGER.exception(
                        "AI price estimate crashed for %s; cycle continues", batch[0][0].key
                    )
                    stats["ai_price_failed"] += len(batch)

        for batch, outcome in outcomes:
            listing, analysis = batch[0]
            if outcome.call_log:
                self.storage.log_ai_call(outcome.call_log)
                stats["ai_price_calls"] += 1
                stats["ai_price_cost_usd"] = round(
                    stats["ai_price_cost_usd"]
                    + float(outcome.call_log.get("estimated_total_cost_usd", 0.0)),
                    6,
                )
            if outcome.estimate.status == "PRICE_OK":
                _key, kind = estimate_key(listing, analysis.identity, analysis.ai_analysis)
                self.storage.cache_ai_price(
                    listing, outcome.estimate, kind, self.config.ai.price_cache_ttl_hours
                )
            for index, (item, item_analysis) in enumerate(batch):
                estimate = outcome.estimate if index == 0 else replace(outcome.estimate, cache_used=True)
                self._apply_price(item, item_analysis, estimate, stats)
                if index:
                    stats["ai_price_cache_hits"] += 1

    def _apply_price(
        self,
        listing: Listing,
        analysis: ListingAnalysis,
        estimate: AIPriceEstimate,
        stats: dict[str, int],
    ) -> None:
        analysis.ai_price = estimate
        if estimate.status == "PRICE_REJECTED":
            stats["ai_price_rejected"] += 1
            return
        if estimate.status != "PRICE_OK":
            stats["ai_price_failed"] += 1
            return
        # Реальные аналоги не подменяются: оценка заполняет пустоту, а не спорит
        # с измерением.
        if analysis.market_valuation is not None and analysis.market_valuation.market_price_czk is not None:
            return
        valuation = to_market_valuation(
            estimate, listing, self.config.market_pricing.quick_sale_discount
        )
        if valuation is None:
            return
        analysis.market_valuation = valuation
        analysis.used_comparables = valuation.as_used_comparables()
        stats["ai_price_applied"] += 1

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

    def _enrich_cyklobazar_descriptions(self, new_listings: list[Listing]) -> int:
        """Догрузить полное описание с детальной страницы Cyklobazar.

        В списке площадка отдаёт только обрывок, а характеристики — материал
        рамы, вилка, трансмиссия, размер колёс — живут на странице объявления.
        Без них по этому источнику пусто и у каталога, и у AI: в карточке
        остаётся один заголовок.

        Запросы ограничены бюджетом на цикл, а первая же HTTP-ошибка обрывает
        догрузку — при 403 от Cloudflare продолжать значит напрашиваться на бан.
        """

        budget = self.config.cyklobazar_detail_budget
        if budget <= 0:
            return 0
        candidates = [item for item in new_listings if item.source == "cyklobazar"]
        enriched = 0
        for index, listing in enumerate(candidates[:budget]):
            if index:
                time.sleep(0.5)
            try:
                description = self._duplicate_detail_description(listing)
            except HttpError as exc:
                LOGGER.warning(
                    "Cyklobazar detail fetch stopped after %d of %d listings: %s",
                    enriched,
                    len(candidates),
                    exc,
                )
                break
            if len(description) <= len(listing.description):
                continue
            listing.description = description[:2000]
            self.storage.update_listing_description(listing)
            enriched += 1
        if len(candidates) > budget:
            LOGGER.info(
                "Cyklobazar detail budget spent: %d of %d new listings enriched",
                enriched,
                len(candidates),
            )
        return enriched

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
        # Описание догружается до анализа: и каталог, и AI читают уже полный текст.
        detail_fetches = self._enrich_cyklobazar_descriptions(new_listings)
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

        # Фаза AI Level 1. В shadow-режиме результат только сохраняется: ни
        # статусы, ни бюджет скрейпинга, ни Telegram его не читают.
        # Bootstrap-подавление (suppress_keys) — про Telegram, а не про дубликаты:
        # если передать его сюда как "дубликаты", на первом запуске AI пропустит
        # 62 из 63 объявлений и больше никогда к ним не вернётся, потому что на
        # следующем цикле они уже не будут "new". Исключаем только подтверждённые
        # cross-source дубликаты.
        ai_stats = self._run_ai_analysis(analyzed, duplicate_suppressed_keys)

        # AI Opportunity Gate. Считается до распределения слотов рыночного
        # анализа: именно он и решает, кому эти слоты достанутся.
        gate_stats = self._run_ai_gate(analyzed)

        lookup_budget = (
            dynamic_lookup_budget(len(new_listings), self.config.priority)
            if finder or market_finder
            else 0
        )
        lookup_pool = [
            value for key, value in analyzed.items()
            if key not in suppress_keys and key not in duplicate_suppressed_keys
        ]
        lookups = select_lookup_candidates(
            lookup_pool,
            lookup_budget,
            use_analysis_priority=self.config.ai_gate.enabled,
        )
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
            # build_analysis создаёт объект заново, поэтому результаты AI-фазы
            # нужно перенести явно — иначе они теряются у тех объявлений,
            # которым достался слот глубокого анализа.
            analysis.ai_analysis = previous.ai_analysis
            analysis.ai_price = previous.ai_price
            analysis.analysis_priority_score = previous.analysis_priority_score
            analysis.analysis_priority_reasons = previous.analysis_priority_reasons
            analysis.ai_gate_action = previous.ai_gate_action
            if consecutive_errors and market_valuation is None:
                analysis.preliminary_priority_score = max(
                    analysis.preliminary_priority_score,
                    self.config.priority.manual_review_min_score,
                )
                analysis.priority_class = "manual_review"
            analyzed[listing.key] = (listing, analysis)
            self.storage.save_analysis(listing, analysis)

        # Фаза AI Level 2: цена там, где детерминированный каскад её не дал.
        price_stats = self._run_ai_price_estimates(analyzed)

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

        ai_suppressed_keys = self._ai_suppressed_keys(analyzed)

        now = datetime.now(UTC)
        notification_pool: list[tuple[Listing, ListingAnalysis, float]] = []
        for key, (listing, analysis) in analyzed.items():
            if key in suppress_keys:
                continue
            if key in duplicate_suppressed_keys:
                continue
            if key in ai_suppressed_keys:
                # AI уверенно решил, что это не велосипед (кресло, велотуфли,
                # тренажёр, деталь) — карточка не уходит, но причина остаётся в базе.
                analysis.notification_status = "excluded"
                analysis.notification_reason = "ai_not_a_bicycle"
                self.storage.save_analysis(listing, analysis)
                continue
            first_seen = self.storage.first_seen_at(listing)
            age_hours = max(0.0, (now - first_seen.astimezone(UTC)).total_seconds() / 3600)
            notification_pool.append((listing, analysis, age_hours))
        telegram_gate_stats: dict[str, int] = {}
        if self.config.deal_scoring.enabled:
            telegram_gate_stats = self._run_telegram_gate(notification_pool)
            eligible = (
                [item for item in notification_pool if item[1].telegram_gate_action == SEND]
                if self.config.telegram_gate.enabled
                else notification_pool
            )
            selected = select_deal_notifications(
                eligible,
                self.config.deal_scoring,
                max_cards=self.config.priority.max_telegram_cards,
                manual_review_reserved_slots=self.config.priority.manual_review_reserved_slots,
                max_age_hours=self.config.priority.individual_notification_max_age_hours,
            )
        else:
            selected = select_notifications(notification_pool, self.config.priority)
        selected_keys = {listing.key for listing, _ in selected}

        sent = 0
        sent_by_status: dict[str, int] = {}
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
                if analysis.deal_evaluation is not None:
                    status = analysis.deal_evaluation.status
                    sent_by_status[status] = sent_by_status.get(status, 0) + 1
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
                elif analysis.telegram_gate_action == BLOCK:
                    status = "excluded"
                    reason = f"telegram_gate_{analysis.telegram_gate_reason or 'blocked'}"
                elif analysis.telegram_gate_action not in {"", SEND}:
                    status = "not_selected"
                    reason = f"telegram_gate_{analysis.telegram_gate_reason or 'skipped'}"
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
            "cyklobazar_detail_fetches": detail_fetches,
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
            "hot_sent": sent_by_status.get("HOT", 0),
            "interesting_sent": sent_by_status.get("INTERESTING", 0),
            "manual_review_sent": sent_by_status.get("MANUAL_REVIEW", 0),
        }
        for source, count in market_http_requests.items():
            stats[f"market_http_{source}"] = count
        stats.update(ai_stats)
        stats.update(price_stats)
        stats.update(gate_stats)
        stats.update(telegram_gate_stats)
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
