"""Дневной бюджет расходов на OpenAI (раздел 17 ТЗ).

Источник истины — сумма ``estimated_total_cost_usd`` в ``ai_call_log`` с
начала текущих UTC-суток. Отдельного счётчика в памяти нет намеренно: после
перезапуска процесса бюджет обязан помнить уже потраченное.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time

from deal_radar.config import AIConfig

LOGGER = logging.getLogger(__name__)


def start_of_day(now: datetime | None = None) -> datetime:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return datetime.combine(moment.date(), time.min, tzinfo=UTC)


@dataclass(slots=True)
class BudgetState:
    spent_usd: float
    limit_usd: float
    calls: int
    stopped: bool
    warned: bool

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    @property
    def percent_used(self) -> float:
        if self.limit_usd <= 0:
            return 0.0
        return round(self.spent_usd / self.limit_usd * 100, 2)


class BudgetGuard:
    def __init__(self, config: AIConfig, storage) -> None:
        self.config = config
        self.storage = storage

    def state(self, now: datetime | None = None) -> BudgetState:
        since = start_of_day(now)
        spent = self.storage.ai_spend_usd_since(since)
        limit = self.config.daily_budget_usd
        over_limit = limit > 0 and spent >= limit
        return BudgetState(
            spent_usd=round(spent, 6),
            limit_usd=limit,
            calls=self.storage.ai_call_count_since(since),
            stopped=bool(over_limit and self.config.stop_at_budget),
            warned=bool(limit > 0 and spent >= limit * self.config.warn_at_percent / 100),
        )

    def check(self, now: datetime | None = None) -> BudgetState:
        """Состояние бюджета с однократным логированием на цикл."""

        state = self.state(now)
        if state.stopped:
            LOGGER.warning(
                "AI daily budget reached: $%.4f of $%.2f spent in %d calls; new listings stay AI_PENDING",
                state.spent_usd,
                state.limit_usd,
                state.calls,
            )
        elif state.warned:
            LOGGER.warning(
                "AI daily budget at %.1f%%: $%.4f of $%.2f spent in %d calls",
                state.percent_used,
                state.spent_usd,
                state.limit_usd,
                state.calls,
            )
        return state
