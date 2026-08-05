from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from deal_radar.config import AppConfig, RetailConfig, SearchProfile, TelegramConfig
from deal_radar.http import HttpError
from deal_radar.models import Listing
from deal_radar.service import DealRadarService


class _RateLimitedSource:
    label = "bad"

    def __init__(self) -> None:
        self.profile = SearchProfile(name="bad", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")
        self.calls = 0

    def fetch(self) -> list[Listing]:
        self.calls += 1
        raise HttpError("GET failed: HTTP Error 403: Forbidden")


class _GoodSource:
    label = "good"

    def __init__(self) -> None:
        self.profile = SearchProfile(name="good", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")

    def fetch(self) -> list[Listing]:
        return [
            Listing(
                source="bazos", external_id="g1", title="Trek Marlin",
                description="ok kolo", url="https://bazos.cz/inzerat/1/k.php",
                profile="good", price_czk=9000, published_at=datetime.now(UTC),
            )
        ]


def _config(path: Path, **overrides) -> AppConfig:
    return AppConfig(
        database_path=str(path),
        profiles=[SearchProfile(name="x", rss_url="https://sport.bazos.cz/rss.php?hledat=kolo")],
        telegram=TelegramConfig(bot_token="t", chat_id="1"),
        retail=RetailConfig(enabled=False),
        **overrides,
    )


class SourceBackoffTest(unittest.TestCase):
    def test_rate_limited_source_paused_others_continue(self):
        with TemporaryDirectory() as directory:
            service = DealRadarService(_config(Path(directory) / "s.db"))
            bad, good = _RateLimitedSource(), _GoodSource()
            service.sources = [bad, good]
            try:
                first = service.fetch_all()
                self.assertEqual(len(first), 1)
                self.assertIn("bad", service._source_backoff)

                second = service.fetch_all()
                self.assertEqual(bad.calls, 1)  # на паузе, повторно не вызывался
                self.assertEqual(len(second), 1)  # рабочий источник продолжает
            finally:
                service.close()

    def test_backoff_grows_exponentially(self):
        with TemporaryDirectory() as directory:
            service = DealRadarService(_config(Path(directory) / "s.db", poll_interval_seconds=600))
            bad = _RateLimitedSource()
            service.sources = [bad, _GoodSource()]
            try:
                service.fetch_all()
                first_delay = service._source_backoff["bad"][1]
                self.assertEqual(first_delay, 600)
                service._source_backoff["bad"] = (0.0, first_delay)  # снять паузу
                service.fetch_all()
                self.assertEqual(service._source_backoff["bad"][1], 1200)
            finally:
                service.close()

    def test_jitter_within_bounds(self):
        with TemporaryDirectory() as directory:
            service = DealRadarService(
                _config(Path(directory) / "s.db", poll_interval_seconds=420, poll_jitter_seconds=60)
            )
            try:
                for _ in range(50):
                    interval = service._next_interval()
                    self.assertGreaterEqual(interval, 360)
                    self.assertLessEqual(interval, 480)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
