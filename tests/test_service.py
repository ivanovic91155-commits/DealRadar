from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from deal_radar.config import AppConfig, RetailConfig, SearchProfile, TelegramConfig
from deal_radar.bike_identity import identify_bike
from deal_radar.models import Listing
from deal_radar.service import DealRadarService


class FakeSource:
    def __init__(self, listings: list[Listing]) -> None:
        self.listings = listings
        self.profile = SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")

    def fetch(self) -> list[Listing]:
        return self.listings


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_listing(self, listing: Listing, retail_enabled: bool) -> int:
        self.sent.append(listing.external_id)
        return 100 + len(self.sent)

    def poll_feedback(self, offset: int = 0):
        return [], offset


def make_listing(external_id: str, minutes: int) -> Listing:
    return Listing(
        source="bazos",
        external_id=external_id,
        title=f"Bike {external_id}",
        description="Complete bike",
        url=f"https://sport.bazos.cz/inzerat/{external_id}/bike.php",
        profile="test",
        published_at=datetime.now(UTC) + timedelta(minutes=minutes),
    )


class ServiceBootstrapTest(unittest.TestCase):
    def test_first_run_sends_only_latest_then_only_new_ids(self) -> None:
        with TemporaryDirectory() as directory:
            config = AppConfig(
                database_path=str(Path(directory) / "state.sqlite3"),
                bootstrap_mode="send_latest",
                max_initial_notifications=1,
                profiles=[SearchProfile(name="test", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")],
                telegram=TelegramConfig(bot_token="test", chat_id="1"),
                retail=RetailConfig(enabled=False),
            )
            service = DealRadarService(config)
            source = FakeSource([make_listing("1", 1), make_listing("2", 2), make_listing("3", 3)])
            telegram = FakeTelegram()
            service.sources = [source]
            service._telegram = lambda: telegram  # type: ignore[method-assign]
            try:
                first = service.process_once()
                self.assertEqual(first["sent"], 1)
                self.assertEqual(telegram.sent, ["3"])

                second = service.process_once()
                self.assertEqual(second["sent"], 0)

                source.listings.append(make_listing("4", 4))
                third = service.process_once()
                self.assertEqual(third["sent"], 1)
                self.assertEqual(telegram.sent, ["3", "4"])
            finally:
                service.close()

    def test_price_failure_does_not_block_listing_alert(self) -> None:
        class FailingFinder:
            def identify(self, listing):
                return identify_bike("Trek Marlin 7 Gen 3 29 2025")

            def find(self, listing, identity):
                raise RuntimeError("price source failed")

        with TemporaryDirectory() as directory:
            config = AppConfig(
                database_path=str(Path(directory) / "state.sqlite3"),
                profiles=[SearchProfile(name="test", rss_url="https://www.bazos.cz/rss.php?rub=sp")],
                telegram=TelegramConfig(bot_token="test", chat_id="1"),
                retail=RetailConfig(enabled=True),
            )
            service = DealRadarService(config)
            telegram = FakeTelegram()
            service.sources = [FakeSource([make_listing("9", 1)])]
            service._telegram = lambda: telegram  # type: ignore[method-assign]
            service._retail_finder = lambda: FailingFinder()  # type: ignore[method-assign]
            try:
                result = service.process_once()
                self.assertEqual(result["sent"], 1)
                self.assertEqual(result["enriched"], 0)
                self.assertEqual(telegram.sent, ["9"])
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
