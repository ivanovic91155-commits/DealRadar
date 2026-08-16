from __future__ import annotations

import unittest

from deal_radar.ai.prefilter import (
    REASON_DUPLICATE,
    REASON_KIDS,
    REASON_NO_PRICE,
    REASON_PARTS,
    REASON_PRICE_RANGE,
    REASON_WANTED,
    ai_prefilter,
)
from deal_radar.config import AIConfig
from deal_radar.models import Listing

CONFIG = AIConfig()


def listing(title: str, description: str = "", price_czk: int | None = 15000) -> Listing:
    return Listing(
        source="bazos",
        external_id="1",
        profile="test",
        title=title,
        url="https://example.test/1",
        description=description,
        price_czk=price_czk,
    )


class RejectionTest(unittest.TestCase):
    def test_accessory_listing_is_rejected_via_the_existing_hard_filter(self) -> None:
        result = ai_prefilter(listing("Author Vision sada kol 29"), CONFIG)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, REASON_PARTS)
        self.assertEqual(result.matched_rule, "hard_filter_accessory_or_part")

    def test_bike_shaped_non_bikes_are_rejected_before_spending_a_call(self) -> None:
        # Слово "kolo" в заголовке протаскивает вещи, велосипедом не являющиеся:
        # это ровно те карточки, на которые жаловался владелец.
        for title in (
            "Dětská sedačka na kolo",
            "Spinningove kolo SCHWINN",
            "Tretry na kolo FORCE, vel. 45",
            "Cyklotrenažér Elite Suito",
            "Rotoped domácí skládací",
        ):
            with self.subTest(title=title):
                result = ai_prefilter(listing(title), CONFIG)
                self.assertFalse(result.passed, title)
                self.assertEqual(result.reason_code, REASON_PARTS)

    def test_protective_gear_is_rejected_before_spending_a_call(self) -> None:
        # Шлем POC Axion доехал до Telegram как «🚲 POC Axion»: в списке был
        # английский "helmet", а объявления на Bazoši чешские.
        for title in (
            "Helma POC Axion 55-58",
            "Cyklistická přilba Uvex",
            "Brýle Rudy Project",
            "Cyklistické rukavice Sportful",
            "Zámek na kolo Abus",
            "Nosič na kolo Thule",
        ):
            with self.subTest(title=title):
                result = ai_prefilter(listing(title), CONFIG)
                self.assertFalse(result.passed, title)
                self.assertEqual(result.reason_code, REASON_PARTS)

    def test_the_na_kolo_phrase_is_matched_in_its_most_common_form(self) -> None:
        # Правило писалось под "sedačka na kolo", но регулярка ловила только
        # "na kola"/"na kol": самая частая форма винительного падежа проходила.
        for title in ("Držák telefonu na kolo", "Blikačka na kolo", "Košík na kolo"):
            with self.subTest(title=title):
                self.assertFalse(ai_prefilter(listing(title), CONFIG).passed, title)

    def test_gear_next_to_a_real_bicycle_does_not_lose_the_listing(self) -> None:
        # Продавец кладёт шлем в подарок — велосипед от этого не перестаёт быть
        # велосипедом. "adresa" содержит "dres": короткие слова в списке
        # экипировки утащили бы за собой настоящее объявление.
        for title in (
            "Prodám kolo Trek Marlin 7 + helma zdarma",
            "Horské kolo Merida s nosičem a blatníky",
            "Elektrokolo Haibike, v ceně zámek a brýle",
            "Trek Marlin 7, adresa v popisu",
        ):
            with self.subTest(title=title):
                self.assertTrue(ai_prefilter(listing(title), CONFIG).passed, title)

    def test_real_bicycles_are_not_caught_by_the_accessory_backstop(self) -> None:
        # "na kolo" ловит аксессуары, но настоящий велосипед в заголовке зовётся
        # "kolo"/"kola", а не "na kolo"; размер колёс "na 29 kolech" — не аксессуар.
        for title in (
            "Trek Marlin 7 2024 horské kolo",
            "Prodám kolo",
            "Kross Level s nosičem na 29 kolech",
            "Scott Aspect, jezdí na 27.5 kolech",
        ):
            with self.subTest(title=title):
                result = ai_prefilter(listing(title), CONFIG)
                self.assertTrue(result.passed, title)

    def test_kids_bike_is_rejected(self) -> None:
        result = ai_prefilter(listing("Woom 4"), CONFIG)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, REASON_KIDS)

    def test_confirmed_duplicate_is_rejected_before_any_other_rule(self) -> None:
        result = ai_prefilter(listing("Trek Marlin 7"), CONFIG, is_duplicate=True)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, REASON_DUPLICATE)

    def test_wanted_ads_are_rejected_in_several_languages(self) -> None:
        for title in (
            "Koupím horské kolo Trek",
            "Kúpim bicykel Scott",
            "Sháním gravel Cube",
            "Hledám kolo pro syna",
            "Wanted: Trek Marlin",
            "Suche Mountainbike Cube",
            "Kupię rower Kross",
        ):
            with self.subTest(title=title):
                result = ai_prefilter(listing(title), CONFIG)
                self.assertFalse(result.passed, title)
                self.assertEqual(result.reason_code, REASON_WANTED)
                self.assertTrue(result.matched_rule.startswith("wanted_keyword:"))

    def test_price_below_and_above_the_profile_range_is_rejected(self) -> None:
        low = ai_prefilter(listing("Trek Marlin 7", price_czk=500), CONFIG, min_price_czk=3000)
        high = ai_prefilter(listing("Trek Marlin 7", price_czk=900000), CONFIG, max_price_czk=90000)
        self.assertEqual(low.reason_code, REASON_PRICE_RANGE)
        self.assertEqual(low.matched_rule, "below_min:3000")
        self.assertEqual(high.reason_code, REASON_PRICE_RANGE)
        self.assertEqual(high.matched_rule, "above_max:90000")

    def test_missing_price_is_rejected_only_when_the_policy_asks_for_it(self) -> None:
        strict = AIConfig(skip_listings_without_price=True)
        self.assertTrue(ai_prefilter(listing("Trek Marlin 7", price_czk=None), CONFIG).passed)
        rejected = ai_prefilter(listing("Trek Marlin 7", price_czk=None), strict)
        self.assertFalse(rejected.passed)
        self.assertEqual(rejected.reason_code, REASON_NO_PRICE)


class PermissivenessTest(unittest.TestCase):
    """Раздел 4 ТЗ: фильтр не должен быть чрезмерно строгим."""

    def test_short_vague_listing_reaches_the_ai(self) -> None:
        self.assertTrue(ai_prefilter(listing("Prodám Scott, málo jeté, spěchá"), CONFIG).passed)

    def test_listing_without_a_model_reaches_the_ai(self) -> None:
        # После этапа 1 модель у таких объявлений пустая — это и есть повод для AI.
        self.assertTrue(ai_prefilter(listing("Trek horské kolo ve skvělém stavu"), CONFIG).passed)

    def test_listing_without_a_brand_reaches_the_ai(self) -> None:
        self.assertTrue(ai_prefilter(listing("Kolo po synovi, 29 palců"), CONFIG).passed)

    def test_negotiable_price_reaches_the_ai_by_default(self) -> None:
        self.assertTrue(
            ai_prefilter(listing("Cube Aim, cena dohodou", price_czk=None), CONFIG).passed
        )

    def test_buy_word_inside_a_normal_sale_description_does_not_reject(self) -> None:
        # «koupím» встречается в описании обычной продажи; заголовок решает.
        result = ai_prefilter(
            listing("Prodám Trek Marlin 7", description="Koupím si nový, proto prodávám tento."),
            CONFIG,
        )
        self.assertTrue(result.passed)


class LogShapeTest(unittest.TestCase):
    def test_rejected_result_serialises_to_the_documented_log_entry(self) -> None:
        entry = ai_prefilter(listing("Koupím kolo"), CONFIG).to_dict()
        self.assertEqual(sorted(entry), ["checked_at", "matched_rule", "passed", "reason_code"])
        self.assertIs(entry["passed"], False)
        self.assertEqual(entry["reason_code"], REASON_WANTED)
        self.assertTrue(entry["checked_at"].startswith("20"))

    def test_passing_result_carries_no_reason(self) -> None:
        entry = ai_prefilter(listing("Trek Marlin 7"), CONFIG).to_dict()
        self.assertIs(entry["passed"], True)
        self.assertEqual(entry["reason_code"], "")
        self.assertEqual(entry["matched_rule"], "")


if __name__ == "__main__":
    unittest.main()
