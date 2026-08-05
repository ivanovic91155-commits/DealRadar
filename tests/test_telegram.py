from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from deal_radar.config import AppConfig, RetailConfig, SearchProfile, TelegramConfig
from deal_radar.http import HttpError
from deal_radar.models import (
    BikeIdentity,
    DealEvaluation,
    Listing,
    ListingAnalysis,
    MarketComparable,
    MarketValuation,
    RetailOffer,
    UsedComparables,
    Valuation,
)
from deal_radar.service import DealRadarService
from deal_radar.telegram import TelegramClient, format_seller_price


class CaptureTelegram(TelegramClient):
    def __init__(self) -> None:
        super().__init__("test", "1")
        self.last_text = ""

    def send_text(self, text: str, reply_to_message_id: int | None = None) -> int:
        self.last_text = text
        return 99


class ApiTelegram(TelegramClient):
    def __init__(self, updates=None, fail_methods=None) -> None:
        super().__init__("test", "1")
        self.updates = updates or []
        self.calls = []
        self.fail_methods = set(fail_methods or [])

    def _call(self, method, fields):
        self.calls.append((method, fields))
        if method in self.fail_methods:
            raise HttpError(f"failed {method}")
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
        self.assertIn("Ориентировочная цена нового", telegram.last_text)
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
        self.assertIn("Подтверждённая цена", telegram.last_text)
        self.assertIn("не используется", telegram.last_text)


class TelegramFeedbackTest(unittest.TestCase):
    def test_stage_2_2_card_extends_existing_pipeline_and_keeps_buttons(self) -> None:
        listing = Listing(
            source="bazos",
            external_id="stage22",
            title="Trek Marlin 7",
            description="Po servisu a bez investic",
            url="https://www.bazos.cz/inzerat/stage22/trek-marlin.php",
            profile="Praha",
            price_czk=12000,
            price_amount=12000,
            price_status="numeric",
        )
        market = MarketValuation(
            listing_source="bazos",
            listing_external_id="stage22",
            market_price_czk=20000,
            quick_sale_price_czk=17000,
            price_low_czk=18000,
            price_high_czk=22000,
            confidence="medium",
            valuation_method="weighted_median",
            status="market_price_found",
            comparables_total=4,
            comparables_unique=3,
            exact_comparables=2,
            close_comparables=1,
            cz_comparables=3,
            countries_used=["CZ"],
            sources_used=["bazos_cz"],
            comparables=[
                MarketComparable(
                    source="bazos_cz",
                    source_listing_id="comparable",
                    url="https://www.bazos.cz/inzerat/comparable/trek.php",
                    country="CZ",
                    title="Trek Marlin 7 comparable",
                    price_original=19500,
                    currency="CZK",
                    price_czk=19500,
                    match_type="exact",
                    match_score=0.95,
                )
            ],
        )
        deal = DealEvaluation(
            listing_source="bazos",
            listing_external_id="stage22",
            algorithm_version="2.2.0",
            status="HOT",
            purchase_price_czk=12000,
            risk_reserve_czk=1200,
            total_investment_czk=13200,
            market_median_czk=20000,
            quick_sale_price_czk=17000,
            net_profit_czk=3800,
            roi_percent=28.79,
            liquidity_score=60,
            liquidity_level="medium",
            estimated_days_to_sell=45,
            confidence_score=65,
            confidence_level="medium",
            deal_score=74.5,
            explanation="Выполнен сценарий HOT по правилу ROI.",
        )
        item_analysis = ListingAnalysis(
            72,
            "interesting_candidate",
            identity=BikeIdentity(brand="Trek", model="Marlin 7"),
            market_valuation=market,
            deal_evaluation=deal,
        )
        telegram = ApiTelegram()
        telegram.send_listing(listing, retail_enabled=False, analysis=item_analysis)
        method, fields = telegram.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertIn("🔥 HOT · 74/100", fields["text"])
        self.assertIn("🔥 HOT · 74/100", fields["text"])
        self.assertIn("Навар:", fields["text"])
        self.assertIn("📈 Навар:", fields["text"])
        self.assertIn("~+3 800 Kč", fields["text"])
        self.assertIn("https://www.bazos.cz/inzerat/comparable/trek.php", fields["text"])
        keyboard = json.loads(fields["reply_markup"])
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
            if "callback_data" in button
        ]
        self.assertIn("fb:i:b:stage22", callbacks)
        self.assertIn("fb:n:b:stage22", callbacks)
        self.assertIn("fb:e:b:stage22", callbacks)

    def test_stage_2_2_manual_review_question_is_shown_first_as_warning(self) -> None:
        listing = self._listing()
        item_analysis = ListingAnalysis(
            40,
            "manual_review",
            identity=BikeIdentity(brand="Trek", model="Marlin 7"),
            deal_evaluation=DealEvaluation(
                listing_source=listing.source,
                listing_external_id=listing.external_id,
                algorithm_version="2.2.0",
                status="MANUAL_REVIEW",
                purchase_price_czk=10000,
                quick_sale_price_czk=17000,
                total_investment_czk=11000,
                net_profit_czk=6000,
                roi_percent=54.55,
                liquidity_level="medium",
                confidence_level="low",
                deal_score=51,
                explanation="Оценка рынка имеет низкую уверенность.",
                manual_review_question="Проверьте необходимость обслуживания вилки.",
            ),
        )
        telegram = ApiTelegram()
        telegram.send_listing(listing, retail_enabled=False, analysis=item_analysis)
        self.assertIn("MANUAL REVIEW", telegram.calls[0][1]["text"])
        self.assertIn(
            "Проверьте необходимость обслуживания вилки.",
            telegram.calls[0][1]["text"],
        )
        text = telegram.calls[0][1]["text"]
        self.assertIn("Проверьте необходимость обслуживания вилки.", text)

    def test_stage_1_2_card_contains_priority_sources_used_warning_and_old_buttons(self) -> None:
        listing = Listing(
            source="cyklobazar",
            external_id="stage12",
            title="Trek Marlin 7",
            description="",
            url="https://www.cyklobazar.cz/inzerat/stage12/trek-marlin",
            profile="Praha",
            price_czk=14900,
            price_amount=14900,
            price_status="numeric",
        )
        offer = RetailOffer("Shop A", "Trek Marlin 7", 23000, "https://shop.example/trek")
        valuation = Valuation(
            identified_product="Trek Marlin 7",
            confidence="low",
            status="success",
            comparables=[offer],
            median_price_czk=23000,
            source_count=1,
            independent_source_count=1,
            offer_count=1,
            new_price_confidence="low",
            discount_percent=35,
        )
        analysis = ListingAnalysis(
            72,
            "interesting_candidate",
            reasons=["Модель распознана."],
            risks=["Цена нового велосипеда основана на одном источнике."],
            identity=BikeIdentity(brand="Trek", model="Marlin 7"),
            valuation=valuation,
            used_comparables=UsedComparables(
                count=3,
                minimum_price_czk=16000,
                maximum_price_czk=19000,
                median_price_czk=17000,
                confidence="medium",
            ),
            duplicate_alternatives=[
                {
                    "source": "bazos",
                    "external_id": "other",
                    "url": "https://sport.bazos.cz/inzerat/other/bike.php",
                }
            ],
        )
        telegram = ApiTelegram()
        telegram.send_listing(
            listing,
            retail_enabled=False,
            diagnostic_header="stage_1_2",
            analysis=analysis,
        )
        method, fields = telegram.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertIn("Проверка DealRadar — этап 1.2", fields["text"])
        self.assertIn("Потенциально интересно · 72/100", fields["text"])
        self.assertIn("🆕 Новый:", fields["text"])
        self.assertIn("Открыть объявление", fields["text"])
        self.assertIn("Открыть объявление", fields["text"])
        self.assertIn("https://shop.example/trek", fields["text"])
        keyboard = json.loads(fields["reply_markup"])
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
            if "callback_data" in button
        ]
        self.assertIn("fb:i:c:stage12", callbacks)

    def test_listing_buttons_carry_marketplace_source(self) -> None:
        listing = Listing(
            source="cyklobazar",
            external_id="0dbED7vqmwQ60",
            title="Rock Machine 29 XL",
            description="",
            url="https://www.cyklobazar.cz/inzerat/0dbED7vqmwQ60/rock-machine",
            profile="Praha",
            price_czk=9990,
            price_amount=9990,
            price_status="numeric",
            price_origin="list_page",
        )
        telegram = ApiTelegram()

        telegram.send_listing(
            listing,
            retail_enabled=False,
            diagnostic_header="Проверка Cyklobazar — этап 1.1",
        )

        method, fields = telegram.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertIn("Новое на Cyklobazar", fields["text"])
        self.assertIn("🧪 <b>Проверка Cyklobazar</b>\nЭтап 1.1", fields["text"])
        self.assertIn("Цена продавца: 9 990 Kč", fields["text"])
        self.assertNotIn("Источник цены", fields["text"])
        keyboard = json.loads(fields["reply_markup"])
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
            if "callback_data" in button
        ]
        self.assertIn("fb:i:c:0dbED7vqmwQ60", callbacks)
        self.assertEqual(keyboard["inline_keyboard"][-1][0]["url"], listing.url)

    def test_formats_non_numeric_seller_price_statuses_without_zero(self) -> None:
        listing = self._listing()
        for status, expected in (
            ("negotiable", "договорная"),
            ("free", "бесплатно"),
            ("in_description", "указана в описании"),
            ("missing", "не указана"),
            ("parse_error", "не удалось распознать"),
        ):
            with self.subTest(status=status):
                listing.price_status = status
                listing.price_czk = None
                listing.price_amount = None
                self.assertEqual(format_seller_price(listing), expected)

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

    @staticmethod
    def _listing() -> Listing:
        return Listing(
            source="cyklobazar",
            external_id="0dbED7vqmwQ60",
            title="Rock Machine 29 XL",
            description="",
            url="https://www.cyklobazar.cz/inzerat/0dbED7vqmwQ60/rock-machine",
            profile="Praha",
            price_czk=9990,
            price_amount=9990,
            price_status="numeric",
            price_origin="list_page",
        )

    @staticmethod
    def _update(data: str, query_id: str = "q1", *, photo: bool = False, update_id: int = 10):
        message = {
            "message_id": 77,
            "chat": {"id": 1},
            "reply_markup": {"inline_keyboard": [[{"text": "Open", "url": "https://example.test"}]]},
        }
        if photo:
            message.update({"caption": "Original caption", "photo": [{"file_id": "photo"}]})
        else:
            message["text"] = "Original text"
        return {
            "update_id": update_id,
            "callback_query": {"id": query_id, "data": data, "from": {"id": 42}, "message": message},
        }

    def _service(self, directory: str) -> DealRadarService:
        return DealRadarService(
            AppConfig(
                database_path=str(Path(directory) / "state.sqlite3"),
                profiles=[SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")],
                telegram=TelegramConfig(bot_token="test", chat_id="1"),
                retail=RetailConfig(enabled=False),
            )
        )

    def test_interesting_is_saved_and_edits_text_removing_classification_buttons(self) -> None:
        with TemporaryDirectory() as directory:
            service = self._service(directory)
            listing = self._listing()
            service.storage.register([listing])
            telegram = ApiTelegram([self._update("fb:i:c:0dbED7vqmwQ60")])
            try:
                self.assertEqual(service.collect_feedback(telegram), 1)
                label = service.storage.connection.execute("SELECT label FROM feedback").fetchone()["label"]
                self.assertEqual(label, "interesting")
                methods = [method for method, _ in telegram.calls]
                self.assertLess(methods.index("answerCallbackQuery"), methods.index("editMessageText"))
                fields = next(fields for method, fields in telegram.calls if method == "editMessageText")
                self.assertIn("⭐ Отмечено: интересно", fields["text"])
                keyboard = json.loads(fields["reply_markup"])
                self.assertTrue(keyboard["inline_keyboard"][0][0]["url"].startswith("https://"))
                self.assertNotIn("callback_data", json.dumps(keyboard))
            finally:
                service.close()

    def test_interesting_edits_photo_caption(self) -> None:
        listing = self._listing()
        telegram = ApiTelegram([self._update("fb:i:c:0dbED7vqmwQ60", photo=True)])
        events, _ = telegram.get_feedback_updates(0)
        telegram.apply_feedback_action(events[0], listing)
        fields = next(fields for method, fields in telegram.calls if method == "editMessageCaption")
        self.assertIn("⭐ Отмечено: интересно", fields["caption"])

    def test_skip_and_too_expensive_are_saved_separately_and_delete(self) -> None:
        for code, expected in (("n", "skip"), ("e", "too_expensive")):
            with self.subTest(code=code), TemporaryDirectory() as directory:
                service = self._service(directory)
                listing = self._listing()
                service.storage.register([listing])
                telegram = ApiTelegram([self._update(f"fb:{code}:c:0dbED7vqmwQ60")])
                try:
                    service.collect_feedback(telegram)
                    label = service.storage.connection.execute("SELECT label FROM feedback").fetchone()["label"]
                    self.assertEqual(label, expected)
                    self.assertIn("deleteMessage", [method for method, _ in telegram.calls])
                finally:
                    service.close()

    def test_delete_error_falls_back_to_visual_marker(self) -> None:
        listing = self._listing()
        telegram = ApiTelegram(
            [self._update("fb:e:c:0dbED7vqmwQ60")],
            fail_methods={"deleteMessage"},
        )
        events, _ = telegram.get_feedback_updates(0)
        telegram.apply_feedback_action(events[0], listing)
        fields = next(fields for method, fields in telegram.calls if method == "editMessageText")
        self.assertIn("💸 Мимо: дорого", fields["text"])

    def test_repeated_callback_does_not_duplicate_feedback_or_delete(self) -> None:
        with TemporaryDirectory() as directory:
            service = self._service(directory)
            service.storage.register([self._listing()])
            update = self._update("fb:n:c:0dbED7vqmwQ60", query_id="same")
            telegram = ApiTelegram([update])
            try:
                service.collect_feedback(telegram)
                service.storage.set_metadata("telegram_update_offset", "0")
                service.collect_feedback(telegram)
                count = service.storage.connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
                self.assertEqual(count, 1)
                self.assertEqual([m for m, _ in telegram.calls].count("deleteMessage"), 1)
            finally:
                service.close()

    def test_unknown_label_is_ignored_and_following_callback_continues(self) -> None:
        updates = [
            self._update("fb:x:c:bad", query_id="bad", update_id=10),
            self._update("fb:i:c:0dbED7vqmwQ60", query_id="good", update_id=11),
        ]
        telegram = ApiTelegram(updates)
        events, next_offset = telegram.get_feedback_updates(0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["label"], "interesting")
        self.assertEqual(next_offset, 12)

    def test_polling_continues_after_callback_api_error(self) -> None:
        with TemporaryDirectory() as directory:
            service = self._service(directory)
            service.storage.register([self._listing()])
            telegram = ApiTelegram(
                [
                    self._update("fb:n:c:0dbED7vqmwQ60", query_id="q1", update_id=10),
                    self._update("fb:i:c:0dbED7vqmwQ60", query_id="q2", update_id=11),
                ],
                fail_methods={"deleteMessage"},
            )
            try:
                service.collect_feedback(telegram)
                self.assertEqual(service.storage.connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 2)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
