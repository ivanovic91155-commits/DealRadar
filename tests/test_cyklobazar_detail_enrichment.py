"""Догрузка описаний с детальных страниц Cyklobazar.

В списке площадка отдаёт только обрывок текста, поэтому характеристики
(материал рамы, вилка, размер колёс) до анализа не доезжали вовсе.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from deal_radar.config import (
    AppConfig,
    CyklobazarProfile,
    MarketPricingConfig,
    RetailConfig,
    SearchProfile,
    TelegramConfig,
)
from deal_radar.http import HttpError
from deal_radar.models import Listing
from deal_radar.service import DealRadarService

DETAIL = (
    'Zachovalé horské kolo 27,5" 4Ever Sauron, drobné provozní oděrky. '
    'Velikost rámu 15,5" - S. Rám Hliník. Odpružená vidlice Suntour XCM30. '
    "Pohon Shimano Acera 3x8. Brzdy hydraulické kotoučové Tektro."
)


class FakeSource:
    def __init__(self, listings: list[Listing]) -> None:
        self.listings = listings
        self.profile = SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")

    def fetch(self) -> list[Listing]:
        return self.listings


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_listing(self, item: Listing, retail_enabled: bool, analysis=None) -> int:
        self.sent.append(item.external_id)
        return 100 + len(self.sent)

    def poll_feedback(self, offset: int = 0):
        return [], offset


def listing(external_id: str, source: str = "cyklobazar", description: str = "4EVER Sauron 5 000 Kč") -> Listing:
    return Listing(
        source=source,
        external_id=external_id,
        profile="test",
        title="4EVER Sauron",
        description=description,
        url=f"https://www.cyklobazar.cz/inzerat/{external_id}/kolo",
        price_czk=5000,
        price_amount=5000,
        price_status="numeric",
        published_at=datetime.now(UTC),
    )


class EnrichmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.fetched: list[str] = []

    def tearDown(self) -> None:
        self.directory.cleanup()

    def build(self, items: list[Listing], *, budget: int = 20) -> tuple[DealRadarService, FakeTelegram]:
        config = AppConfig(
            database_path=str(Path(self.directory.name) / "state.sqlite3"),
            bootstrap_mode="send_all",
            cyklobazar_detail_budget=budget,
            profiles=[SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")],
            cyklobazar_profiles=[
                CyklobazarProfile(name="cb", url="https://www.cyklobazar.cz/kola", enabled=False)
            ],
            telegram=TelegramConfig(bot_token="test", chat_id="1"),
            retail=RetailConfig(enabled=False),
            market_pricing=MarketPricingConfig(enabled=False),
        )
        config.validate()
        service = DealRadarService(config)
        service.sources = [FakeSource(items)]
        return service, FakeTelegram()

    def install_detail(self, service: DealRadarService, *, error: Exception | None = None) -> None:
        def loader(item: Listing) -> str:
            self.fetched.append(item.external_id)
            if error is not None:
                raise error
            return DETAIL

        service._duplicate_detail_description = loader  # type: ignore[method-assign]

    def test_detail_description_reaches_the_analysis(self) -> None:
        service, telegram = self.build([listing("a")])
        self.install_detail(service)
        try:
            stats = service.process_once(telegram)
            stored = service.storage.get_listing("cyklobazar", "a")
            analysis = service.storage.get_analysis("cyklobazar", "a")
        finally:
            service.close()
        self.assertEqual(self.fetched, ["a"])
        self.assertEqual(stats["cyklobazar_detail_fetches"], 1)
        assert stored is not None
        self.assertIn("Suntour", stored.description)
        # Характеристики из описания доехали до идентификации.
        assert analysis is not None and analysis.identity is not None
        self.assertEqual(analysis.identity.wheel_size, "27.5")

    def test_bazos_listings_are_never_fetched(self) -> None:
        service, telegram = self.build([listing("b", source="bazos")])
        self.install_detail(service)
        try:
            stats = service.process_once(telegram)
        finally:
            service.close()
        self.assertEqual(self.fetched, [])
        self.assertEqual(stats["cyklobazar_detail_fetches"], 0)

    def test_budget_caps_the_number_of_requests(self) -> None:
        service, telegram = self.build([listing(str(i)) for i in range(5)], budget=2)
        self.install_detail(service)
        try:
            stats = service.process_once(telegram)
        finally:
            service.close()
        self.assertEqual(len(self.fetched), 2)
        self.assertEqual(stats["cyklobazar_detail_fetches"], 2)

    def test_zero_budget_disables_the_fetch(self) -> None:
        service, telegram = self.build([listing("a")], budget=0)
        self.install_detail(service)
        try:
            service.process_once(telegram)
        finally:
            service.close()
        self.assertEqual(self.fetched, [])

    def test_http_error_stops_the_run_instead_of_hammering(self) -> None:
        service, telegram = self.build([listing(str(i)) for i in range(5)])
        self.install_detail(service, error=HttpError("GET failed: HTTP Error 403: Forbidden", 403))
        try:
            stats = service.process_once(telegram)
        finally:
            service.close()
        self.assertEqual(len(self.fetched), 1)
        self.assertEqual(stats["cyklobazar_detail_fetches"], 0)
        # Цикл продолжается: объявление всё равно сохранено и разобрано.
        self.assertEqual(stats["new"], 5)

    def test_shorter_detail_text_does_not_overwrite_a_longer_snippet(self) -> None:
        service, telegram = self.build([listing("a", description="x" * 500)])
        self.install_detail(service)
        try:
            service.process_once(telegram)
            stored = service.storage.get_listing("cyklobazar", "a")
        finally:
            service.close()
        assert stored is not None
        self.assertEqual(stored.description, "x" * 500)

    def test_second_cycle_does_not_refetch_the_same_listing(self) -> None:
        items = [listing("a")]
        service, telegram = self.build(items)
        self.install_detail(service)
        try:
            service.process_once(telegram)
            service.process_once(telegram)
        finally:
            service.close()
        # Догружаются только новые объявления, а во втором цикле новых нет.
        self.assertEqual(self.fetched, ["a"])


if __name__ == "__main__":
    unittest.main()
