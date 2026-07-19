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
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
