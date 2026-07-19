from datetime import datetime
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from deal_radar.config import CyklobazarProfile
from deal_radar.http import HttpError
from deal_radar.models import Listing
from deal_radar.sources.cyklobazar import (
    CyklobazarSource,
    parse_detail_price,
    parse_html,
    parse_price_text,
)


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
        self.assertEqual(first.price_amount, 9990)
        self.assertEqual(first.price_status, "numeric")
        self.assertEqual(first.price_origin, "list_page")
        self.assertEqual(first.raw_price_text, "9 990 Kč")
        self.assertEqual(first.location, "Praha")
        self.assertEqual(first.published_at.hour, 11)
        self.assertEqual(first.image_url, "https://www.cyklobazar.cz/media/rock-machine.jpg")
        self.assertIn("/inzerat/0dbED7vqmwQ60/", first.url)

    def test_detects_cloudflare_page_instead_of_silently_returning_empty(self) -> None:
        with self.assertRaises(HttpError):
            parse_html(b"<html><title>Just a moment...</title></html>", self.profile)

    def test_supported_price_formats(self) -> None:
        for raw in (
            "12 500 Kč",
            "12\u00a0500 Kč",
            "12500 Kč",
            "12.500 Kč",
            "12 500,-",
            "Cena: 12 500 CZK včetně příslušenství",
            "12\n500 Kč",
        ):
            with self.subTest(raw=raw):
                parsed = parse_price_text(raw)
                self.assertEqual(parsed.amount, 12500)
                self.assertEqual(parsed.status, "numeric")
                self.assertEqual(parsed.currency, "CZK")

    def test_text_price_statuses_missing_and_parse_error(self) -> None:
        cases = {
            "Dohodou": "negotiable",
            "Cena dohodou": "negotiable",
            "Zdarma": "free",
            "Cena v textu": "in_description",
            "": "missing",
            "Cena: ???": "parse_error",
        }
        for raw, status in cases.items():
            with self.subTest(raw=raw):
                parsed = parse_price_text(raw)
                self.assertIsNone(parsed.amount)
                self.assertEqual(parsed.status, status)

    def test_does_not_treat_year_or_wheel_size_as_price(self) -> None:
        for raw in ('Model 2025, kola 29"', "Rám 56, model 2024"):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_price_text(raw).amount)

    def test_detail_page_prefers_json_ld_offer(self) -> None:
        result = parse_detail_price(
            b'<script type="application/ld+json">'
            b'{"@type":"Product","offers":{"@type":"Offer","price":12500,"priceCurrency":"CZK"}}'
            b'</script>'
        )
        self.assertEqual(result.amount, 12500)
        self.assertEqual(result.origin, "detail_page")

    @staticmethod
    def _missing_price_list() -> bytes:
        return b"""
        <a href="/inzerat/NewBike12345/test-bike" class="advert-card">
          <h3 class="advert-title">Trek Marlin 7</h3><time>pred 1 minutou</time>
        </a>
        """

    def test_falls_back_to_detail_page_for_new_missing_price(self) -> None:
        calls: list[str] = []

        def fetcher(url: str, timeout: int = 30, headers=None) -> bytes:
            calls.append(url)
            if url == self.profile.url:
                return self._missing_price_list()
            return b'<script type="application/ld+json">{"offers":{"price":12500,"priceCurrency":"CZK"}}</script>'

        source = CyklobazarSource(self.profile, fetcher=fetcher)
        listings = source.fetch()
        self.assertEqual(len(calls), 2)
        self.assertEqual(listings[0].price_czk, 12500)
        self.assertEqual(listings[0].price_origin, "detail_page")
        self.assertEqual(source.last_stats.numeric_detail_page, 1)

    def test_list_page_price_does_not_fetch_detail(self) -> None:
        html = self._missing_price_list().replace(
            b"</a>", b'<strong class="advert-price">12 500 K\xc4\x8d</strong></a>'
        )
        calls: list[str] = []

        def fetcher(url: str, timeout: int = 30, headers=None) -> bytes:
            calls.append(url)
            return html

        listing = CyklobazarSource(self.profile, fetcher=fetcher).fetch()[0]
        self.assertEqual(len(calls), 1)
        self.assertEqual(listing.price_czk, 12500)
        self.assertEqual(listing.price_origin, "list_page")

    def test_existing_listing_without_price_does_not_repeat_detail_fetch(self) -> None:
        existing = Listing(
            source="cyklobazar",
            external_id="NewBike12345",
            title="Trek Marlin 7",
            description="",
            url="https://www.cyklobazar.cz/inzerat/NewBike12345/test-bike",
            profile="test",
        )
        calls: list[str] = []

        def fetcher(url: str, timeout: int = 30, headers=None) -> bytes:
            calls.append(url)
            return self._missing_price_list()

        source = CyklobazarSource(
            self.profile,
            existing_listing=lambda external_id: existing,
            fetcher=fetcher,
        )
        source.fetch()
        self.assertEqual(len(calls), 1)

    def test_detail_http_error_and_timeout_do_not_stop_source(self) -> None:
        for error in (HttpError("blocked"), TimeoutError("slow")):
            with self.subTest(error=type(error).__name__):
                def fetcher(url: str, timeout: int = 30, headers=None, error=error) -> bytes:
                    if url == self.profile.url:
                        return self._missing_price_list()
                    raise error

                source = CyklobazarSource(self.profile, fetcher=fetcher)
                listings = source.fetch()
                self.assertEqual(len(listings), 1)
                self.assertIsNone(listings[0].price_czk)
                self.assertEqual(source.last_stats.detail_errors, 1)


if __name__ == "__main__":
    unittest.main()
