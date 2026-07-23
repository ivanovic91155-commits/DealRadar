from __future__ import annotations

import argparse
import json
import logging
import sys

from deal_radar.config import load_config, load_dotenv
from deal_radar.models import Listing
from deal_radar.service import DealRadarService
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
    return parser


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
