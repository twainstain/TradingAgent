"""Signal dataclass.

Strategies return `Signal` objects. Only the Execution Agent (Phase 4) calls
the broker — and only after the Risk Agent (Phase 3) approves the signal.
Strategies never send orders. (CLAUDE.md invariant #2.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    strategy: str
    confidence: float
    reason: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tick_id: int | None = None
    llm_call_id: int | None = None
    llm_branch: str | None = None  # rule_only | llm_approved | llm_rejected (Phase 5)
