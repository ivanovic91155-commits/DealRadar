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
        self.profile = SearchProfile(name="bad", rss_url="https://www.bazos.cz/rss.php?rub=sp")
        self.calls = 0

    def fetch(self):
        self.calls += 1
        raise HttpError("GET failed: HTTP Error 403: Forbidden")


class _GoodSource:
    label = "good"

    def __init__(self) -> None:
        self.profile = SearchProfile(name="good", rss_url="https://www.bazos.cz/rss.php?rub=sp")

    def fetch(self):
        return [
            Listing(
                source="bazos", external_id="g1", title="Trek Marlin",
                description="ok kolo", url="https://bazos.cz/inzerat/1/k.php",
                profile="good", price_czk=9000, published_at=datetime.now(UTC),
            )
        ]


class BackoffTest(unittest.TestCase):
    def test_rate_limited_source_paused_others_continue(self):
        with TemporaryDirectory() as d:
            cfg = AppConfig(
                database_path=str(Path(d) / "s.db"),
                profiles=[SearchProfile(name="x", rss_url="https://www.bazos.cz/rss.php?rub=sp")],
                telegram=TelegramConfig(bot_token="t", chat_id="1"),
                retail=RetailConfig(enabled=False),
            )
            service = DealRadarService(cfg)
            bad, good = _RateLimitedSource(), _GoodSource()
            service.sources = [bad, good]
            try:
                r1 = service.fetch_all()
                self.assertEqual(len(r1), 1)
                self.assertIn("bad", service._source_backoff)

                r2 = service.fetch_all()
                # sбойный источник не вызывается повторно, пока на паузе
                self.assertEqual(bad.calls, 1)
                self.assertEqual(len(r2), 1)
            finally:
                service.close()

    def test_backoff_grows_exponentially(self):
        with TemporaryDirectory() as d:
            cfg = AppConfig(
                database_path=str(Path(d) / "s.db"),
                poll_interval_seconds=420,
                profiles=[SearchProfile(name="x", rss_url="https://www.bazos.cz/rss.php?rub=sp")],
                telegram=TelegramConfig(bot_token="t", chat_id="1"),
                retail=RetailConfig(enabled=False),
            )
            service = DealRadarService(cfg)
            bad = _RateLimitedSource()
            service.sources = [bad, _GoodSource()]
            try:
                service.fetch_all()
                first_delay = service._source_backoff["bad"][1]
                self.assertEqual(first_delay, 420)
                # форсируем окончание паузы и повторяем — задержка должна вырасти
                service._source_backoff["bad"] = (0.0, first_delay)
                service.fetch_all()
                second_delay = service._source_backoff["bad"][1]
                self.assertEqual(second_delay, 840)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
