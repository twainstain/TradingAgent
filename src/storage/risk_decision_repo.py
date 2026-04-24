"""risk_decisions table — approvals AND rejections are both persisted."""
from __future__ import annotations

from datetime import datetime, timezone

from trading_platform.persistence.db import DbConnection


def write_decision(
    db: DbConnection,
    *,
    signal_id: int,
    approved: bool,
    reason: str,
    sized_qty: int | None,
    created_at: datetime | None = None,
) -> int:
    ts = (created_at or datetime.now(timezone.utc)).isoformat()
    cur = db.execute(
        """
        INSERT INTO risk_decisions (signal_id, approved, reason, sized_qty, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (signal_id, 1 if approved else 0, reason, sized_qty, ts),
    )
    db.commit()
    return int(cur.lastrowid)
