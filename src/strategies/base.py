"""Strategy base — shared context type and NaN-safety helpers.

Strategies are pure functions over a `StrategyContext`. They return a
`Signal` or None. They MUST NOT raise on partial/malformed snapshots
(Phase 2 exit criterion). If a required field is missing, return None.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from core.signal import Signal
from storage.snapshot_repo import SnapshotRow


@dataclass(frozen=True)
class StrategyContext:
    """Per-symbol inputs a strategy evaluates.

    Kept separate from SnapshotRow so strategies can consume context fields
    (intraday volume, yesterday's close) that aren't persisted in the
    `snapshots` table.
    """
    snapshot: SnapshotRow
    volume_today: float | None = None
    yesterday_close: float | None = None


def _is_num(x: object) -> bool:
    """True iff x is a finite, non-NaN number."""
    if x is None:
        return False
    try:
        f = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


class Strategy(Protocol):
    """Minimal protocol: a named `evaluate` that returns Signal | None."""
    name: str

    def evaluate(self, ctx: StrategyContext) -> Signal | None: ...
