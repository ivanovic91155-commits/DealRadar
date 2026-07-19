from datetime import datetime
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from deal_radar.config import CyklobazarProfile
from deal_radar.http import HttpError
from deal_radar.sources.cyklobazar import parse_html


FIXTURE = Path(__file__).parent / "fixtures" / "cyklobazar_list.html"


class CyklobazarParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = CyklobazarProfile(
            name="Cyklobazar Prague",
            url="https://www.cyklobazar.cz/kola?filter%5Bloc_district_id%5D=1000",
            location_label="Praha",
            min_price_czk=1000,
            max_price_czk=100000,
        )

    def test_parses_deduplicates_and_filters_listing_cards(self) -> None:
        now = datetime(2026, 7, 17, 12, 0, tzinfo=ZoneInfo("Europe/Prague"))
        listings = parse_html(FIXTURE.read_bytes(), self.profile, now=now)

        self.assertEqual([listing.external_id for listing in listings], ["0dbED7vqmwQ60", "bAqXk6B2JLEMB"])
        first = listings[0]
        self.assertEqual(first.source, "cyklobazar")
        self.assertEqual(first.title, "Horské kolo Rock Machine 29” , XL")
        self.assertEqual(first.price_czk, 9990)
        self.assertEqual(first.location, "Praha")
        self.assertEqual(first.published_at.hour, 11)
        self.assertEqual(first.image_url, "https://www.cyklobazar.cz/media/rock-machine.jpg")
        self.assertIn("/inzerat/0dbED7vqmwQ60/", first.url)

    def test_detects_cloudflare_page_instead_of_silently_returning_empty(self) -> None:
        with self.assertRaises(HttpError):
            parse_html(b"<html><title>Just a moment...</title></html>", self.profile)


if __name__ == "__main__":
    unittest.main()
