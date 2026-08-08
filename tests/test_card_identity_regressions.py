"""Регрессии по двум реальным карточкам из Telegram от 8 августа 2026.

Merida: размер рамы 16" уехал в поле колёс, настоящие 26" из описания потерялись,
модель Juliet не распозналась. 4EVER: бренда не было в списке, а в заголовке
карточки печатался сырой текст объявления вместе с ценой.
"""

from __future__ import annotations

import html
import re
import unittest
from datetime import UTC, datetime

from deal_radar.bike_identity import identify_bike, identify_listing
from deal_radar.config import PriorityConfig
from deal_radar.models import AIAnalysis, AIIdentity, AISpecifications, Listing
from deal_radar.priority import build_analysis
from deal_radar.telegram import _analysis_card_text, _headline, _spec_lines

MERIDA_TITLE = 'Dámské horské kolo MERIDA (vel. 16") super stav: 6 500'
MERIDA_DESC = (
    "Prodám udržované dámské horské kolo značky Merida (modelová řada Juliet). "
    'Velikost rámu 16" (vel. S): Ideální pro postavu s výškou cca 155 až 165 cm. '
    'Kolo jezdí na klasických 26" kolech, která jsou lehká.'
)
SAURON_TITLE = 'Zachovalý Hliníkový 27,5" MTB 4EVER Sauron vel. S (150-165 cm)'
SAURON_DESC = (
    'Zachovalé horské kolo 27,5" 4Ever Sauron, drobné provozní oděrky. '
    'Velikost rámu 15,5" - S pro výšku cca 150 - 165 cm. Rám Hliník.'
)


def listing(title: str, description: str = "", price: int | None = 6500, source: str = "bazos") -> Listing:
    return Listing(
        source=source,
        external_id="1",
        profile="test",
        title=title,
        description=description,
        url="https://example.test/1",
        price_czk=price,
        price_amount=price,
        price_status="numeric",
        location="Praha",
        published_at=datetime.now(UTC),
    )


def analysis_for(item: Listing):
    return build_analysis(item, PriorityConfig(), identity=identify_listing(item))


def plain_card(item: Listing) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", _analysis_card_text(item, analysis_for(item))))


class WheelSizeTest(unittest.TestCase):
    def test_frame_size_in_the_title_is_not_read_as_wheels(self) -> None:
        identity = identify_bike(MERIDA_TITLE, MERIDA_DESC)
        self.assertEqual(identity.wheel_size, "26")
        self.assertEqual(identity.frame_size, "16")

    def test_wheel_size_survives_a_decimal_comma(self) -> None:
        self.assertEqual(identify_bike(SAURON_TITLE, SAURON_DESC).wheel_size, "27.5")

    def test_explicit_wheel_context_wins_over_a_frame_marker(self) -> None:
        self.assertEqual(identify_bike("Kolo Author velikost kol 26").wheel_size, "26")

    def test_plain_wheel_size_still_works(self) -> None:
        for title, expected in (
            ('Trek Marlin 7 2024 29"', "29"),
            ("Woom 4 kolo 20 palcu", "20"),
            ("Cube Aim 27,5", "27.5"),
        ):
            with self.subTest(title=title):
                self.assertEqual(identify_bike(title).wheel_size, expected)

    def test_frame_only_listing_reports_no_wheels(self) -> None:
        identity = identify_bike('Cube Aim vel. 18"')
        self.assertEqual(identity.frame_size, "18")
        self.assertEqual(identity.wheel_size, "")


class CatalogTest(unittest.TestCase):
    def test_model_is_found_in_the_description_not_only_the_title(self) -> None:
        identity = identify_bike(MERIDA_TITLE, MERIDA_DESC)
        self.assertEqual(identity.model, "Juliet")
        self.assertTrue(identity.model_confirmed)
        self.assertEqual(identity.audience, "women")

    def test_czech_brand_4ever_is_recognised(self) -> None:
        identity = identify_bike(SAURON_TITLE, SAURON_DESC)
        self.assertEqual(identity.brand, "4EVER")
        self.assertEqual(identity.model, "Sauron")
        self.assertTrue(identity.model_confirmed)

    def test_rider_height_range_does_not_become_a_model_variant(self) -> None:
        # "(150-165 cm)" — рост ездока; раньше давал модель "Sauron 150".
        self.assertEqual(identify_bike(SAURON_TITLE, SAURON_DESC).model, "Sauron")

    def test_real_numeric_variants_still_attach(self) -> None:
        self.assertEqual(identify_bike("Trek Marlin 7 2024").model, "Marlin 7")


class HeadlineTest(unittest.TestCase):
    def test_headline_carries_the_model_not_the_raw_title(self) -> None:
        item = listing(MERIDA_TITLE, MERIDA_DESC)
        self.assertEqual(_headline(analysis_for(item), item), "Merida Juliet · женский")

    def test_headline_never_repeats_the_price(self) -> None:
        card = plain_card(listing(MERIDA_TITLE, MERIDA_DESC))
        headline = next(line for line in card.splitlines() if line.startswith("🚲"))
        self.assertNotIn("6 500", headline)
        self.assertNotIn("6500", headline)

    def test_headline_drops_frame_and_wheel_noise(self) -> None:
        item = listing(SAURON_TITLE, SAURON_DESC)
        self.assertEqual(_headline(analysis_for(item), item), "4EVER Sauron")

    def test_unknown_model_falls_back_to_a_title_without_the_price(self) -> None:
        item = listing("Prodám kolo po synovi: 3 500", "")
        self.assertNotIn("3 500", _headline(analysis_for(item), item))

    def test_kids_and_electric_specificity_is_shown(self) -> None:
        kids = listing("Woom 4", "")
        self.assertIn("детский", _headline(analysis_for(kids), kids))
        ebike = listing("Haibike AllMtn elektrokolo", "")
        self.assertIn("электро", _headline(analysis_for(ebike), ebike))


class CardLayoutTest(unittest.TestCase):
    def test_blocks_are_separated_by_blank_lines(self) -> None:
        card = plain_card(listing(MERIDA_TITLE, MERIDA_DESC))
        lines = card.splitlines()
        headline_at = next(i for i, line in enumerate(lines) if line.startswith("🚲"))
        # Отступ после заголовка, дальше характеристики, отступ, цена.
        self.assertEqual(lines[headline_at + 1], "")
        self.assertTrue(lines[headline_at + 2].startswith("🏪"))
        price_at = next(i for i, line in enumerate(lines) if line.startswith("💰"))
        self.assertEqual(lines[price_at - 1], "")

    def test_numeric_frame_size_is_shown_in_inches(self) -> None:
        item = listing(MERIDA_TITLE, MERIDA_DESC)
        specs = [html.unescape(line) for line in _spec_lines(analysis_for(item), item)]
        self.assertIn('📐 Рама 16"', specs)
        self.assertIn('🛞 Колёса 26"', specs)

    def test_letter_frame_size_keeps_its_letter(self) -> None:
        item = listing(SAURON_TITLE, SAURON_DESC)
        self.assertIn("📐 Рама S", [html.unescape(x) for x in _spec_lines(analysis_for(item), item)])


class AiFallbackTest(unittest.TestCase):
    """Раздел 22 ТЗ: AI заполняет пробелы отображения, но решения не трогает."""

    def with_ai(self, item: Listing, **ai_fields):
        analysis = analysis_for(item)
        analysis.ai_analysis = AIAnalysis(
            status="AI_OK",
            identity=AIIdentity(brand="Merida", model="Juliet", is_electric=False),
            specifications=AISpecifications(**ai_fields),
        )
        return analysis

    def test_ai_supplies_the_model_when_the_parser_found_none(self) -> None:
        item = listing("Prodám kolo, spěchá", "")
        self.assertEqual(_headline(self.with_ai(item), item), "Merida Juliet")

    def test_ai_supplies_wheels_and_frame_when_the_parser_found_none(self) -> None:
        item = listing("Prodám kolo, spěchá", "")
        specs = [
            html.unescape(line)
            for line in _spec_lines(
                self.with_ai(item, wheel_size_inches=29, frame_size_normalized="M"), item
            )
        ]
        self.assertIn("📐 Рама M", specs)
        self.assertIn('🛞 Колёса 29"', specs)

    def test_parser_wins_when_it_has_an_answer(self) -> None:
        item = listing(MERIDA_TITLE, MERIDA_DESC)
        specs = [html.unescape(x) for x in _spec_lines(self.with_ai(item, wheel_size_inches=29), item)]
        self.assertIn('🛞 Колёса 26"', specs)

    def test_failed_ai_analysis_is_ignored(self) -> None:
        item = listing("Prodám kolo, spěchá", "")
        analysis = analysis_for(item)
        analysis.ai_analysis = AIAnalysis(
            status="AI_FAILED",
            identity=AIIdentity(brand="Merida", model="Juliet"),
        )
        self.assertNotIn("Juliet", _headline(analysis, item))

    def test_card_without_any_ai_still_renders(self) -> None:
        card = plain_card(listing(SAURON_TITLE, SAURON_DESC, source="cyklobazar"))
        self.assertIn("4EVER Sauron", card)
        self.assertIn("Открыть объявление", card)


if __name__ == "__main__":
    unittest.main()
