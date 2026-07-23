from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from deal_radar.exchange_rates import ExchangeRateProvider, parse_cnb_rates
from deal_radar.storage import Storage


CNB_FIXTURE = b"""17 Jul 2026 #136
Country|Currency|Amount|Code|Rate
EMU|euro|1|EUR|24.205
Poland|zloty|1|PLN|5.812
Switzerland|franc|1|CHF|26.500
"""


class ExchangeRateTest(unittest.TestCase):
    def test_parses_eur_pln_and_chf_to_czk(self) -> None:
        rates, valid_date = parse_cnb_rates(CNB_FIXTURE)
        self.assertEqual(valid_date, "2026-07-17")
        self.assertEqual(rates["EUR"], 24.205)
        self.assertEqual(rates["PLN"], 5.812)
        self.assertEqual(rates["CHF"], 26.5)

    def test_cache_hit_does_not_fetch(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "state.sqlite3"))
            storage.save_exchange_rates(
                {"CZK": 1.0, "EUR": 24.2, "PLN": 5.8},
                datetime.now(UTC).date().isoformat(),
                "test",
                datetime.now(UTC).isoformat(),
            )
            provider = ExchangeRateProvider(
                storage,
                "https://example.invalid/rates",
                fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("HTTP must not run")),
            )
            result = provider.get()
            self.assertTrue(result.cache_used)
            self.assertEqual(result.http_requests, 0)
            storage.close()

    def test_unavailable_service_falls_back_to_last_successful_stale_rate(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "state.sqlite3"))
            storage.save_exchange_rates(
                {"CZK": 1.0, "EUR": 24.0, "PLN": 5.7},
                (datetime.now(UTC).date() - timedelta(days=8)).isoformat(),
                "test",
                (datetime.now(UTC) - timedelta(hours=30)).isoformat(),
            )
            provider = ExchangeRateProvider(
                storage,
                "https://example.invalid/rates",
                fetcher=lambda *_: (_ for _ in ()).throw(TimeoutError("offline")),
            )
            result = provider.get()
            self.assertEqual(result.rates_to_czk["EUR"], 24.0)
            self.assertTrue(result.cache_used)
            self.assertTrue(any("старше семи" in warning for warning in result.warnings))
            self.assertTrue(any("последний успешный" in warning for warning in result.warnings))
            storage.close()

    def test_unavailable_service_without_cache_is_reported(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "state.sqlite3"))
            provider = ExchangeRateProvider(
                storage,
                "https://example.invalid/rates",
                fetcher=lambda *_: (_ for _ in ()).throw(TimeoutError("offline")),
            )
            result = provider.get(read_only=True)
            self.assertEqual(result.rates_to_czk, {"CZK": 1.0})
            self.assertTrue(result.warnings)
            self.assertEqual(storage.connection.execute("SELECT COUNT(*) FROM exchange_rates").fetchone()[0], 0)
            storage.close()


if __name__ == "__main__":
    unittest.main()
