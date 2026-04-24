"""fills table — partial/full fills keyed to orders.client_order_id.

Fills arrive via polling (Phase 4 — no public ingress on the paper
endpoint) and are idempotent on (client_order_id, status, filled_qty).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from trading_platform.persistence.db import DbConnection


@dataclass(frozen=True)
class FillRow:
    id: int
    order_id: int
    client_order_id: str
    filled_qty: int
    filled_avg_price: float | None
    status: str
    reported_at: str


def upsert_fill(
    db: DbConnection,
    *,
    order_id: int,
    client_order_id: str,
    filled_qty: int,
    filled_avg_price: float | None,
    status: str,
    reported_at: datetime | None = None,
) -> int:
    """Insert a fill if (client_order_id, status, filled_qty) is new.

    Fills are append-only — we don't update an existing row; instead we
    only insert when any of (status, filled_qty) has changed since the
    last fill we saw for this client_order_id. This gives us a full
    timeline of state transitions for auditing.
    """
    ts = (reported_at or datetime.now(timezone.utc)).isoformat()
    last = db.execute(
        """
        SELECT id, status, filled_qty FROM fills
        WHERE client_order_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (client_order_id,),
    ).fetchone()
    if last is not None and last["status"] == status and last["filled_qty"] == int(filled_qty):
        return int(last["id"])  # unchanged — dedup

    cur = db.execute(
        """
        INSERT INTO fills (order_id, client_order_id, filled_qty, filled_avg_price, status, reported_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            order_id,
            client_order_id,
            int(filled_qty),
            float(filled_avg_price) if filled_avg_price is not None else None,
            status,
            ts,
        ),
    )
    db.commit()
    return int(cur.lastrowid)


def fills_for_order(db: DbConnection, client_order_id: str) -> list[FillRow]:
    rows = db.execute(
        """
        SELECT * FROM fills WHERE client_order_id = ? ORDER BY id ASC
        """,
        (client_order_id,),
    ).fetchall()
    return [
        FillRow(
            id=r["id"],
            order_id=r["order_id"],
            client_order_id=r["client_order_id"],
            filled_qty=r["filled_qty"],
            filled_avg_price=r["filled_avg_price"],
            status=r["status"],
            reported_at=r["reported_at"],
        )
        for r in rows
    ]
