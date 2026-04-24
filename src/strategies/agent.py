"""Strategy Agent — fan out latest non-stale snapshots to each enabled
strategy, merge to one signal per symbol, persist to the `signals` table.

Merge rule (EXECUTION_PLAN §Phase 2): most recent per symbol. Since all
strategies in a tick produce signals at (approximately) the same instant,
we keep the LAST one that evaluated truthy — strategies earlier in the
registry lose to later ones when both fire on the same symbol. That's a
deterministic tiebreak; if we ever care about confidence-weighted merge,
we'll revisit.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from trading_platform.persistence.db import DbConnection

from core.signal import Signal
from storage.bars_repo import load_bars
from storage.signal_repo import write_signals
from storage.snapshot_repo import DEFAULT_FRESHNESS_SECONDS, latest_snapshots
from strategies import load_enabled_strategies
from strategies.base import Strategy, StrategyContext

log = logging.getLogger(__name__)


class StrategyAgent:
    def __init__(
        self,
        db: DbConnection,
        symbols: Iterable[str],
        *,
        strategies: list[Strategy] | None = None,
        freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
    ) -> None:
        self._db = db
        self._symbols = tuple(s.upper() for s in symbols)
        self._strategies = strategies if strategies is not None else load_enabled_strategies()
        self._freshness_seconds = freshness_seconds

    @property
    def strategies(self) -> list[Strategy]:
        return list(self._strategies)

    def _build_context(self, snapshot, now: datetime) -> StrategyContext:
        bars = load_bars(self._db, snapshot.symbol, lookback_days=5)
        volume_today: float | None = None
        yesterday_close: float | None = None
        if len(bars) >= 1:
            volume_today = float(bars["volume"].iloc[-1])
        if len(bars) >= 2:
            yesterday_close = float(bars["close"].iloc[-2])
        return StrategyContext(
            snapshot=snapshot,
            volume_today=volume_today,
            yesterday_close=yesterday_close,
        )

    def evaluate(self, now: datetime | None = None) -> list[Signal]:
        """Fan out to every strategy; merge (most recent per symbol)."""
        now = now or datetime.now(timezone.utc)
        latest = latest_snapshots(
            self._db, self._symbols, freshness_seconds=self._freshness_seconds, now=now
        )
        merged: dict[str, Signal] = {}
        for sym in self._symbols:
            snap = latest.get(sym)
            if snap is None:
                continue
            ctx = self._build_context(snap, now)
            for strat in self._strategies:
                try:
                    sig = strat.evaluate(ctx)
                except Exception:  # noqa: BLE001 — a broken strategy must not kill the tick
                    log.exception("strategy %s raised on %s; dropping", strat.name, sym)
                    continue
                if sig is not None:
                    merged[sym] = sig
        return list(merged.values())

    def run(self, tick_id: int, now: datetime | None = None) -> list[Signal]:
        """Evaluate + persist. Returns the signals that were written."""
        now = now or datetime.now(timezone.utc)
        signals = self.evaluate(now=now)
        if not signals:
            return []
        # Stamp tick_id + rule-only branch on every signal before persisting.
        stamped = [
            Signal(
                symbol=s.symbol,
                side=s.side,
                strategy=s.strategy,
                confidence=s.confidence,
                reason=s.reason,
                created_at=s.created_at,
                tick_id=tick_id,
                llm_branch="rule_only",
            )
            for s in signals
        ]
        write_signals(self._db, stamped)
        return stamped
