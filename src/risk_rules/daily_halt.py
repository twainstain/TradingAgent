"""Daily-loss halt — sticky −2% trip persisted to `risk_state`.

The plan mentions `trading_platform.risk.CircuitBreaker`, but the platform's
breaker is failure/staleness-driven; our trigger is a P&L threshold that
must survive restart. We use the platform's breaker semantically (state +
trip reason) while persisting truth to SQLite so a docker restart on the
same trading day stays halted — only the date rollover clears it.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from trading_platform.persistence.db import DbConnection

from storage.risk_state_repo import (
    RiskState,
    ensure_state,
    get_state,
    is_halted_today,
    mark_halted,
)

log = logging.getLogger(__name__)

DAILY_LOSS_THRESHOLD_PCT = Decimal("0.02")  # 2% of starting equity


class DailyHaltBreaker:
    def __init__(
        self,
        db: DbConnection,
        *,
        loss_threshold_pct: Decimal = DAILY_LOSS_THRESHOLD_PCT,
    ) -> None:
        self._db = db
        self._threshold = loss_threshold_pct

    def check(
        self,
        *,
        trading_date: date,
        starting_equity: Decimal,
        current_equity: Decimal,
        now: datetime | None = None,
    ) -> RiskState:
        """Trip if current equity is ≥ threshold below starting equity.

        Idempotent: if already halted, returns the existing state.
        """
        ensure_state(self._db, trading_date, starting_equity=starting_equity)
        existing = get_state(self._db, trading_date)
        if existing and existing.halted:
            return existing

        loss = starting_equity - current_equity
        loss_pct = loss / starting_equity if starting_equity > 0 else Decimal("0")
        if loss_pct >= self._threshold:
            reason = f"daily_loss_exceeded:{loss_pct * 100:.2f}%"
            log.warning("daily halt engaged: %s", reason)
            return mark_halted(
                self._db,
                trading_date,
                reason,
                engaged_at=now or datetime.now(timezone.utc),
                current_pnl=-loss,
                starting_equity=starting_equity,
            )
        return existing  # type: ignore[return-value]

    def is_halted(self, trading_date: date) -> bool:
        return is_halted_today(self._db, trading_date)
