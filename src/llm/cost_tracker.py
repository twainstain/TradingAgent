"""Daily LLM cost tracker — hard cap `MAX_LLM_DAILY_USD` (default $5).

Reads the day's accumulated cost from `llm_calls` (SQLite truth) on every
check, so the cap survives restart. Above the cap, the judge stage is
skipped and rule-only signals keep flowing.

Pricing (Haiku 4.5 defaults, `.env`-overridable for testing):
  - Input  tokens: $1.00 per 1M
  - Cached input:  $0.10 per 1M
  - Output tokens: $5.00 per 1M

If the model is switched to Sonnet 4.6 (Phase 7), override these in env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from trading_platform.persistence.db import DbConnection

from storage.llm_call_repo import cost_today

DEFAULT_CAP_USD = Decimal("5")

# Haiku 4.5 pricing. All per 1M tokens.
HAIKU_PRICE_IN_USD_PER_1M = Decimal("1.00")
HAIKU_PRICE_CACHED_IN_USD_PER_1M = Decimal("0.10")
HAIKU_PRICE_OUT_USD_PER_1M = Decimal("5.00")


@dataclass(frozen=True)
class CostEstimate:
    tokens_in: int
    tokens_out: int
    cache_hit: bool
    cost_usd: Decimal


def estimate_cost(
    tokens_in: int,
    tokens_out: int,
    *,
    cache_hit: bool = False,
    model: str | None = None,  # reserved for Sonnet 4.6 override (Phase 7)
) -> CostEstimate:
    price_in = HAIKU_PRICE_CACHED_IN_USD_PER_1M if cache_hit else HAIKU_PRICE_IN_USD_PER_1M
    cost = (
        Decimal(tokens_in) * price_in / Decimal("1000000")
        + Decimal(tokens_out) * HAIKU_PRICE_OUT_USD_PER_1M / Decimal("1000000")
    )
    return CostEstimate(tokens_in, tokens_out, cache_hit, cost)


class DailyCostTracker:
    def __init__(
        self,
        db: DbConnection,
        *,
        cap_usd: Decimal | None = None,
    ) -> None:
        self._db = db
        self._cap = cap_usd if cap_usd is not None else Decimal(
            os.environ.get("MAX_LLM_DAILY_USD", str(DEFAULT_CAP_USD))
        )

    @property
    def cap_usd(self) -> Decimal:
        return self._cap

    def spent_today(self, trading_date: date) -> Decimal:
        return cost_today(self._db, trading_date)

    def is_over_cap(self, trading_date: date) -> bool:
        return self.spent_today(trading_date) >= self._cap

    def remaining_today(self, trading_date: date) -> Decimal:
        return max(Decimal("0"), self._cap - self.spent_today(trading_date))
