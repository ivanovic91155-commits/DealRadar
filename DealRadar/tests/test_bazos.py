from pathlib import Path
import unittest

from deal_radar.config import SearchProfile
from deal_radar.sources.bazos import matches_profile, parse_rss


FIXTURE = Path(__file__).parent / "fixtures" / "bazos_rss.xml"


class BazosParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = SearchProfile(
            name="Prague bikes",
            rss_url="https://sport.bazos.cz/rss.php?hledat=kolo",
            require_any_keywords=["kolo", "bike", "elektrokolo"],
            exclude_title_keywords=["vidlice"],
            min_price_czk=1000,
            max_price_czk=100000,
        )
        self.listings = parse_rss(FIXTURE.read_bytes(), self.profile)

    def test_parses_main_fields(self) -> None:
        listing = self.listings[0]
        self.assertEqual(listing.external_id, "222333444")
        self.assertEqual(listing.price_czk, 12500)
        self.assertIn("Praha 4", listing.location or "")
        self.assertTrue((listing.image_url or "").endswith("222333444.jpg"))

    def test_filters_parts_by_title(self) -> None:
        self.assertTrue(matches_profile(self.listings[0], self.profile))
        self.assertFalse(matches_profile(self.listings[1], self.profile))

    def test_allows_missing_price(self) -> None:
        self.assertIsNone(self.listings[2].price_czk)
        self.assertTrue(matches_profile(self.listings[2], self.profile))

    def test_parses_price_from_current_bazos_title_format(self) -> None:
        self.assertEqual(self.listings[3].price_czk, 9000)

    def test_does_not_treat_scooter_as_kolo_keyword(self) -> None:
        scooter = self.listings[0]
        scooter.title = "Elektrokoloběžka X1000"
        scooter.description = "Výkonná městská koloběžka"
        self.assertFalse(matches_profile(scooter, self.profile))


if __name__ == "__main__":
    unittest.main()
