from __future__ import annotations

from datetime import UTC, datetime
import json
import unittest

from deal_radar.models import Listing, RetailOffer, Valuation
from deal_radar.telegram import TelegramClient


class CaptureTelegram(TelegramClient):
    def __init__(self) -> None:
        super().__init__("test", "1")
        self.last_text = ""

    def send_text(self, text: str, reply_to_message_id: int | None = None) -> int:
        self.last_text = text
        return 99


class ApiTelegram(TelegramClient):
    def __init__(self, updates=None) -> None:
        super().__init__("test", "1")
        self.updates = updates or []
        self.calls = []

    def _call(self, method, fields):
        self.calls.append((method, fields))
        if method == "getUpdates":
            return self.updates
        if method in {"sendMessage", "sendPhoto"}:
            return {"message_id": 77}
        return True


class TelegramValuationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.listing = Listing(
            source="bazos",
            external_id="1",
            title="Trek Marlin 7 Gen 3 2025",
            description="",
            url="https://sport.bazos.cz/inzerat/1/bike.php",
            profile="test",
            price_czk=14900,
        )

    def test_formats_success_block_and_limits_links(self) -> None:
        offers = [
            RetailOffer(f"Shop {index}", "Trek Marlin 7", 22000 + index * 1000, f"https://s{index}.example/bike")
            for index in range(4)
        ]
        valuation = Valuation(
            identified_product="Trek Marlin 7 Gen 3 2025",
            confidence="high",
            status="success",
            comparables=offers,
            median_price_czk=22990,
            source_count=4,
            discount_percent=35,
            checked_at=datetime.now(UTC).isoformat(),
        )
        telegram = CaptureTelegram()
        telegram.send_valuation(self.listing, valuation, 1, max_sources=3)
        self.assertIn("Медианная цена нового", telegram.last_text)
        self.assertIn("дешевле на 35%", telegram.last_text)
        self.assertEqual(telegram.last_text.count("https://"), 3)

    def test_formats_insufficient_data_honestly(self) -> None:
        valuation = Valuation(
            identified_product="Trek Marlin 7",
            confidence="low",
            status="insufficient_data",
            source_count=1,
        )
        telegram = CaptureTelegram()
        telegram.send_valuation(self.listing, valuation, 1)
        self.assertIn("минимум 3 точных", telegram.last_text)
        self.assertIn("не используется", telegram.last_text)


class TelegramFeedbackTest(unittest.TestCase):
    def test_listing_buttons_carry_marketplace_source(self) -> None:
        listing = Listing(
            source="cyklobazar",
            external_id="0dbED7vqmwQ60",
            title="Rock Machine 29 XL",
            description="",
            url="https://www.cyklobazar.cz/inzerat/0dbED7vqmwQ60/rock-machine",
            profile="Praha",
            price_czk=9990,
        )
        telegram = ApiTelegram()

        telegram.send_listing(listing, retail_enabled=False)

        method, fields = telegram.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertIn("Новое на Cyklobazar", fields["text"])
        keyboard = json.loads(fields["reply_markup"])
        callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertIn("fb:i:c:0dbED7vqmwQ60", callbacks)

    def test_feedback_supports_new_sources_and_old_bazos_buttons(self) -> None:
        updates = [
            {
                "update_id": 10,
                "callback_query": {
                    "id": "q1",
                    "data": "fb:i:c:0dbED7vqmwQ60",
                    "from": {"username": "buyer"},
                },
            },
            {
                "update_id": 11,
                "callback_query": {
                    "id": "q2",
                    "data": "fb:n:123456789",
                    "from": {"id": 42},
                },
            },
        ]
        telegram = ApiTelegram(updates)

        feedback, next_offset = telegram.poll_feedback(0)

        self.assertEqual(next_offset, 12)
        self.assertEqual(feedback[0]["source"], "cyklobazar")
        self.assertEqual(feedback[0]["label"], "interesting")
        self.assertEqual(feedback[1]["source"], "bazos")
        self.assertEqual(feedback[1]["label"], "skip")
        self.assertEqual([method for method, _ in telegram.calls].count("answerCallbackQuery"), 2)


if __name__ == "__main__":
    unittest.main()
