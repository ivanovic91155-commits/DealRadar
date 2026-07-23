from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from deal_radar.http import get_bytes


CNB_SOURCE = "Czech National Bank"


class ExchangeRateStorage(Protocol):
    def load_exchange_rates(self) -> tuple[dict[str, float], str, str]: ...

    def save_exchange_rates(
        self,
        rates: dict[str, float],
        valid_date: str,
        source: str,
        fetched_at: str,
    ) -> None: ...


@dataclass(slots=True)
class ExchangeRateResult:
    rates_to_czk: dict[str, float]
    valid_date: str
    source: str
    cache_used: bool = False
    warnings: list[str] = field(default_factory=list)
    http_requests: int = 0


def parse_cnb_rates(data: bytes) -> tuple[dict[str, float], str]:
    lines = [line.strip() for line in data.decode("utf-8", errors="replace").splitlines() if line.strip()]
    if len(lines) < 3 or "Code|Rate" not in lines[1]:
        raise ValueError("CNB response has an unexpected format")
    try:
        valid_date = datetime.strptime(lines[0].split("#", 1)[0].strip(), "%d %b %Y").date().isoformat()
    except ValueError as exc:
        raise ValueError("CNB response has an invalid date") from exc
    rates = {"CZK": 1.0}
    for line in lines[2:]:
        parts = line.split("|")
        if len(parts) != 5:
            continue
        try:
            amount = float(parts[2])
            rate = float(parts[4].replace(",", "."))
        except ValueError:
            continue
        code = parts[3].upper()
        if amount > 0 and rate > 0:
            rates[code] = rate / amount
    if "EUR" not in rates or "PLN" not in rates:
        raise ValueError("CNB response is missing required currencies")
    return rates, valid_date


class ExchangeRateProvider:
    def __init__(
        self,
        storage: ExchangeRateStorage,
        url: str,
        ttl_hours: int = 24,
        timeout: int = 20,
        fetcher: Callable[[str, int], bytes] = get_bytes,
    ) -> None:
        self.storage = storage
        self.url = url
        self.ttl_hours = max(1, ttl_hours)
        self.timeout = timeout
        self.fetcher = fetcher

    @staticmethod
    def _age_warning(valid_date: str) -> list[str]:
        try:
            age = datetime.now(UTC).date() - datetime.fromisoformat(valid_date).date()
        except ValueError:
            return ["Дата валютного курса не распознана."]
        return ["Валютный курс старше семи дней."] if age.days > 7 else []

    def get(self, *, read_only: bool = False) -> ExchangeRateResult:
        cached_rates, cached_valid_date, cached_fetched_at = self.storage.load_exchange_rates()
        if cached_rates and cached_fetched_at:
            try:
                fetched = datetime.fromisoformat(cached_fetched_at)
                if fetched.tzinfo is None:
                    fetched = fetched.replace(tzinfo=UTC)
                if fetched + timedelta(hours=self.ttl_hours) > datetime.now(UTC):
                    return ExchangeRateResult(
                        cached_rates,
                        cached_valid_date,
                        CNB_SOURCE,
                        cache_used=True,
                        warnings=self._age_warning(cached_valid_date),
                    )
            except ValueError:
                pass
        try:
            rates, valid_date = parse_cnb_rates(self.fetcher(self.url, self.timeout))
            fetched_at = datetime.now(UTC).isoformat()
            if not read_only:
                self.storage.save_exchange_rates(rates, valid_date, CNB_SOURCE, fetched_at)
            return ExchangeRateResult(
                rates,
                valid_date,
                CNB_SOURCE,
                warnings=self._age_warning(valid_date),
                http_requests=1,
            )
        except Exception as exc:
            if cached_rates:
                warnings = self._age_warning(cached_valid_date)
                warnings.append(f"Сервис валют недоступен; используется последний успешный курс ({type(exc).__name__}).")
                return ExchangeRateResult(
                    cached_rates,
                    cached_valid_date,
                    CNB_SOURCE,
                    cache_used=True,
                    warnings=warnings,
                    http_requests=1,
                )
            return ExchangeRateResult(
                {"CZK": 1.0},
                "",
                CNB_SOURCE,
                warnings=[f"Сервис валют недоступен и сохранённого курса нет ({type(exc).__name__})."],
                http_requests=1,
            )
