from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from deal_radar.ai.client import AIUnavailable, OpenAIClient, estimate_cost_usd
from deal_radar.ai.listing_analysis import ListingAnalyzer
from deal_radar.ai.prefilter import ai_prefilter
from deal_radar.ai.price_estimate import PriceEstimator
from deal_radar.ai.prompt_loader import PromptNotFound, load_prompt
from deal_radar.bike_identity import identify_listing
from deal_radar.config import load_config, load_dotenv
from deal_radar.http import HttpError
from deal_radar.models import Listing
from deal_radar.service import DealRadarService
from deal_radar.sources.facebook import FacebookMarketplaceSource
from deal_radar.telegram import TelegramClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Private multi-marketplace bicycle deal radar")
    parser.add_argument("--config", default="config.json", help="Path to JSON config")
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preview = subparsers.add_parser(
        "preview", help="Fetch enabled sources and print matching listings without changing state"
    )
    preview.add_argument("--limit", type=int, default=10)
    subparsers.add_parser("once", help="Fetch once, deduplicate, send new listings, and exit")
    subparsers.add_parser("run", help="Run continuously")
    subparsers.add_parser("telegram-test", help="Send a simple Telegram test message")
    subparsers.add_parser("chat-id", help="Show chat IDs from messages sent to the bot")
    price_check = subparsers.add_parser("price-check", help="Test new-bike price search without Telegram or state changes")
    price_check.add_argument("--title", required=True, help="Bicycle listing title")
    price_check.add_argument("--description", default="")
    price_check.add_argument("--price", type=int, default=None, help="Listing price in CZK")
    market_check = subparsers.add_parser(
        "market-price-check",
        help="Run Market Price Engine v2 for one manual listing without Telegram",
    )
    market_check.add_argument("--title", required=True)
    market_check.add_argument("--description", default="")
    market_check.add_argument("--price", type=int, default=None)
    market_check.add_argument("--write-cache", action="store_true")
    market_check.add_argument("--force", action="store_true")
    diagnostic = subparsers.add_parser(
        "market-diagnostic",
        help="Evaluate active listings safely; optionally send diagnostic Telegram cards",
    )
    diagnostic.add_argument("--limit", type=int, default=5)
    diagnostic.add_argument("--send-telegram", action="store_true")
    diagnostic.add_argument("--write-state", action="store_true")
    diagnostic.add_argument("--force", action="store_true")
    ai_check = subparsers.add_parser(
        "ai-check", help="Check the AI setup; --live sends one tiny structured-output request"
    )
    ai_check.add_argument("--live", action="store_true", help="Make one real API call")
    ai_listing = subparsers.add_parser(
        "ai-test-listing",
        help="Analyse one listing with AI; no Telegram, no state changes",
    )
    ai_listing.add_argument("--fixture", help="JSON file with title/description/price")
    ai_listing.add_argument("--title", default="")
    ai_listing.add_argument("--description", default="")
    ai_listing.add_argument("--price", type=int, default=None)
    ai_price = subparsers.add_parser(
        "ai-price-check",
        help="Estimate the resale price of one listing with AI; no Telegram, no state changes",
    )
    ai_price.add_argument("--fixture", help="JSON file with title/description/price")
    ai_price.add_argument("--title", default="")
    ai_price.add_argument("--description", default="")
    ai_price.add_argument("--price", type=int, default=None)
    facebook = subparsers.add_parser(
        "facebook-check",
        help="Spend one ScrapeCreators credit on a single search; no Telegram, no state changes",
    )
    facebook.add_argument("--profile", help="Profile name from config.json; default is the first active one")
    facebook.add_argument("--limit", type=int, default=5, help="How many normalised listings to print")
    return parser


def _ai_check(config, live: bool) -> int:
    """Диагностика раздела 20 ТЗ. Ключ печатается только как факт наличия."""

    ai = config.ai
    print(f"AI enabled: {'yes' if ai.enabled else 'no (AI_ANALYSIS_ENABLED)'}")
    print(f"API key configured: {'yes' if ai.api_key else 'no (OPENAI_API_KEY)'}")
    print(f"Shadow mode: {'yes' if ai.shadow_mode else 'no'}")
    print(f"Primary model configured: {ai.model_primary}")
    print(f"Fallback model configured: {ai.model_fallback if ai.fallback_enabled else 'disabled'}")
    print(f"Daily budget: ${ai.daily_budget_usd:.2f} (stop at budget: {ai.stop_at_budget})")
    if ai.price_estimate_enabled:
        print(f"Price estimates: enabled ({ai.price_model}, HOT allowed: {ai.price_allow_hot})")
    else:
        print("Price estimates: disabled (AI_PRICE_ESTIMATE_ENABLED)")
    try:
        bundle = load_prompt(ai.prompt_name, ai.prompt_version, ai.prompts_path)
    except PromptNotFound as exc:
        print(f"Prompt version: FAILED — {exc}")
        return 1
    print(f"Prompt version: {bundle.label}")
    print(f"Schema version: {bundle.schema_version}")
    if not ai.api_key:
        print("Structured Output test: skipped (no API key)")
        return 1
    if not live:
        print("Structured Output test: skipped (pass --live to make one real request)")
        return 0
    try:
        result = OpenAIClient(ai).structured(
            system="Return JSON that matches the schema. This is a connectivity probe.",
            user='Report ok=true. DATA: {"probe": true}',
            schema_name="probe",
            schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["ok"],
                "properties": {"ok": {"type": "boolean"}},
            },
            max_output_tokens=64,
        )
    except AIUnavailable as exc:
        print(f"Structured Output test: FAILED — {exc}")
        return 1
    cost = estimate_cost_usd(
        ai,
        used_fallback=result.used_fallback,
        input_tokens=result.input_tokens,
        cached_input_tokens=result.cached_input_tokens,
        output_tokens=result.output_tokens,
    )
    print(f"Structured Output test: passed ({result.model_name}, {result.total_tokens} tokens, ${cost:.6f})")
    return 0


def _ai_test_listing(config, args) -> int:
    """Безопасный прогон одного объявления: без Telegram и без записи в БД."""

    listing = _manual_listing(args, "ai-test-listing")
    if listing is None:
        return 1
    identity = identify_listing(listing)
    prefilter = ai_prefilter(listing, config.ai, identity=identity)
    print(json.dumps({"prefilter": prefilter.to_dict()}, ensure_ascii=False, indent=2))
    if not prefilter.passed:
        print("Listing rejected before the API call; nothing was spent.")
        return 0
    try:
        analyzer = ListingAnalyzer(config.ai)
    except PromptNotFound as exc:
        print(f"Prompt could not be loaded: {exc}")
        return 1
    outcome = analyzer.analyze(listing, identity)
    print(json.dumps(outcome.analysis.to_dict(), ensure_ascii=False, indent=2))
    log = outcome.call_log or {}
    print(
        "tokens: input={input_tokens} cached={cached_input_tokens} output={output_tokens} "
        "| cost: ${estimated_total_cost_usd:.6f} | model: {model_name} | {latency_ms} ms".format(
            input_tokens=log.get("input_tokens", 0),
            cached_input_tokens=log.get("cached_input_tokens", 0),
            output_tokens=log.get("output_tokens", 0),
            estimated_total_cost_usd=float(log.get("estimated_total_cost_usd", 0.0)),
            model_name=log.get("model_name", ""),
            latency_ms=log.get("latency_ms", 0),
        )
    )
    print("No Telegram message was sent and no production data was changed.")
    return 0 if outcome.analysis.status == "AI_OK" else 1


def _manual_listing(args, external_id: str) -> Listing | None:
    data = {"title": args.title, "description": args.description, "price_czk": args.price}
    if args.fixture:
        data.update(json.loads(Path(args.fixture).read_text(encoding="utf-8")))
    if not data.get("title"):
        print("Provide --fixture or --title")
        return None
    return Listing(
        source="manual",
        external_id=external_id,
        title=str(data["title"]),
        description=str(data.get("description", "")),
        url=f"https://example.invalid/{external_id}",
        profile="manual",
        price_czk=data.get("price_czk"),
        location=data.get("location"),
    )


def _ai_price_check(config, args) -> int:
    """Прогон оценки цены на одном объявлении: один вызов, без записи в прод."""

    listing = _manual_listing(args, "ai-price-check")
    if listing is None:
        return 1
    identity = identify_listing(listing)
    try:
        analyzer = ListingAnalyzer(config.ai)
        estimator = PriceEstimator(config.ai)
    except PromptNotFound as exc:
        print(f"Prompt could not be loaded: {exc}")
        return 1
    # Level 1 сначала: оценка цены сильно зависит от состояния и характеристик.
    analysis = analyzer.analyze(listing, identity).analysis
    print(f"Level 1: {analysis.status}")
    outcome = estimator.estimate(listing, identity, analysis)
    estimate = outcome.estimate
    print(json.dumps(estimate.to_dict(), ensure_ascii=False, indent=2))
    if estimate.status == "PRICE_OK":
        quick = int(round((estimate.market_price_czk or 0) * (1 - config.market_pricing.quick_sale_discount)))
        print(
            f"\nasking: {listing.price_czk} CZK | AI: {estimate.market_price_czk} CZK "
            f"({estimate.price_low_czk}–{estimate.price_high_czk}) | quick sale: {quick} CZK "
            f"| confidence: {estimate.confidence} | basis: {estimate.basis}"
        )
    elif estimate.status == "PRICE_REJECTED":
        print(f"\nEstimate rejected by the sanity check: {estimate.reject_reason}")
    log = outcome.call_log or {}
    print(
        "cost: ${:.6f} | model: {} | {} ms".format(
            float(log.get("estimated_total_cost_usd", 0.0)),
            log.get("model_name", ""),
            log.get("latency_ms", 0),
        )
    )
    print("No Telegram message was sent and no production data was changed.")
    return 0 if estimate.status == "PRICE_OK" else 1


def _facebook_check(config, args) -> int:
    """Ручной smoke-test: ровно один поисковый запрос, ровно один credit.

    В стандартный test suite не входит и в цикл не вмешивается: базу не
    открывает, Telegram не трогает, ничего не сохраняет.
    """

    facebook = config.facebook_marketplace
    if not facebook.api_key:
        print("SCRAPECREATORS_API_KEY is empty; export it before running this check")
        return 1
    profiles = facebook.profiles if args.profile else facebook.active_profiles
    if args.profile:
        profiles = [profile for profile in profiles if profile.name == args.profile]
    if not profiles:
        print("No matching Facebook Marketplace profile in the config")
        return 1
    profile = profiles[0]
    source = FacebookMarketplaceSource(profile, facebook)
    print(f"Profile: {profile.name} | query={profile.query!r} radius={profile.radius_km}km")
    try:
        listings = source.fetch()
    except HttpError as exc:
        # Сообщение об ошибке ключа не содержит: его не печатает и http-слой.
        print(f"Search failed: {exc}")
        return 1
    stats = source.last_stats
    print(
        f"HTTP ok | pages={stats.pages} listings={stats.listings} kept={stats.kept} "
        f"credits_charged={stats.credits_charged:.0f} "
        f"credits_remaining={'unknown' if stats.credits_remaining is None else int(stats.credits_remaining)} "
        f"duration={stats.duration_ms}ms"
    )
    for listing in listings[: max(0, args.limit)]:
        price = (
            f"{listing.price_amount} {listing.currency}"
            if listing.price_amount is not None
            else listing.price_status
        )
        print(
            f"  [{listing.external_id}] {listing.title[:60]} | {price} | "
            f"czk={listing.price_czk} | {listing.location or '-'} | {listing.url}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    if args.command == "chat-id":
        config = load_config(args.config)
        client = TelegramClient(config.telegram.bot_token, timeout=config.request_timeout_seconds)
        chats = client.discover_chat_ids()
        if not chats:
            print("No chats found. Open the bot in Telegram, send /start, then run this command again.")
            return 1
        for chat in chats:
            print(f"TELEGRAM_CHAT_ID={chat['id']}  type={chat['type']}  name={chat['name']}")
        return 0

    config = load_config(args.config)
    if args.command == "telegram-test":
        client = TelegramClient(
            config.telegram.bot_token,
            config.telegram.chat_id,
            timeout=config.request_timeout_seconds,
        )
        message_id = client.send_text("✅ <b>Deal Radar подключен</b>\nTelegram-канал работает.")
        print(f"Test message sent (message_id={message_id})")
        return 0

    # Обе AI-команды работают без DealRadarService: они не открывают базу и не
    # меняют production-данные (раздел 20 ТЗ).
    if args.command == "ai-check":
        return _ai_check(config, args.live)
    if args.command == "ai-test-listing":
        return _ai_test_listing(config, args)
    if args.command == "ai-price-check":
        return _ai_price_check(config, args)
    if args.command == "facebook-check":
        return _facebook_check(config, args)

    service = DealRadarService(config)
    try:
        if args.command == "preview":
            listings = service.preview(args.limit)
            print(f"Matched listings: {len(listings)}")
        elif args.command == "once":
            print(service.process_once())
        elif args.command == "run":
            service.run_forever()
        elif args.command == "price-check":
            finder = service._retail_finder()
            if finder is None:
                raise RuntimeError("Retail price search is disabled in config")
            listing = Listing(
                source="manual",
                external_id="price-check",
                title=args.title,
                description=args.description,
                url="https://example.invalid/manual-price-check",
                profile="manual",
                price_czk=args.price,
            )
            print(json.dumps(finder.find(listing).to_dict(), ensure_ascii=False, indent=2))
        elif args.command == "market-price-check":
            finder = service._market_finder()
            if finder is None:
                raise RuntimeError("Market Price Engine is disabled in config")
            listing = Listing(
                source="manual",
                external_id="market-price-check",
                title=args.title,
                description=args.description,
                url="https://example.invalid/manual-market-price-check",
                profile="manual",
                price_czk=args.price,
                price_amount=args.price,
                price_status="numeric" if args.price else "missing",
            )
            result = finder.find(
                listing,
                read_only=not args.write_cache,
                force=args.force,
            )
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        elif args.command == "market-diagnostic":
            results = service.diagnose_market(
                limit=args.limit,
                send_telegram=args.send_telegram,
                write_state=args.write_state,
                force=args.force,
            )
            summary = [
                {
                    "listing": listing.key,
                    "title": listing.title,
                    "status": analysis.market_valuation.status if analysis.market_valuation else "missing",
                    "market_price_czk": analysis.market_valuation.market_price_czk if analysis.market_valuation else None,
                    "confidence": analysis.market_valuation.confidence if analysis.market_valuation else "none",
                    "countries": analysis.market_valuation.countries_used if analysis.market_valuation else [],
                    "cache_hits": analysis.market_valuation.cache_hits if analysis.market_valuation else 0,
                    "cache_misses": analysis.market_valuation.cache_misses if analysis.market_valuation else 0,
                    "http_requests": analysis.market_valuation.http_requests if analysis.market_valuation else {},
                }
                for listing, analysis in results
            ]
            print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
