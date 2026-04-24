"""orders table — broker submissions keyed by client_order_id."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from trading_platform.persistence.db import DbConnection


@dataclass(frozen=True)
class OrderRow:
    id: int
    risk_decision_id: int
    client_order_id: str
    broker_order_id: str | None
    symbol: str
    side: str
    qty: int
    type: str
    entry_price: float | None
    stop_price: float | None
    take_profit_price: float | None
    status: str
    submitted_at: str
    raw_response: str | None


def _to_iso(ts: datetime | None) -> str:
    return (ts or datetime.now(timezone.utc)).isoformat()


def insert_order(
    db: DbConnection,
    *,
    risk_decision_id: int,
    client_order_id: str,
    broker_order_id: str | None,
    symbol: str,
    side: str,
    qty: int,
    order_type: str,
    entry_price: float | None,
    stop_price: float | None,
    take_profit_price: float | None,
    status: str,
    raw_response: Any | None = None,
    submitted_at: datetime | None = None,
) -> int:
    payload = raw_response
    if payload is not None and not isinstance(payload, str):
        try:
            payload = json.dumps(payload, default=str)
        except Exception:  # noqa: BLE001
            payload = str(payload)
    cur = db.execute(
        """
        INSERT INTO orders (risk_decision_id, client_order_id, broker_order_id,
                            symbol, side, qty, type, entry_price, stop_price,
                            take_profit_price, status, submitted_at, raw_response)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            risk_decision_id,
            client_order_id,
            broker_order_id,
            symbol.upper(),
            side.lower(),
            int(qty),
            order_type,
            float(entry_price) if entry_price is not None else None,
            float(stop_price) if stop_price is not None else None,
            float(take_profit_price) if take_profit_price is not None else None,
            status,
            _to_iso(submitted_at),
            payload,
        ),
    )
    db.commit()
    return int(cur.lastrowid)


def get_order_by_client_id(db: DbConnection, client_order_id: str) -> OrderRow | None:
    row = db.execute(
        "SELECT * FROM orders WHERE client_order_id = ?",
        (client_order_id,),
    ).fetchone()
    if row is None:
        return None
    return OrderRow(
        id=row["id"],
        risk_decision_id=row["risk_decision_id"],
        client_order_id=row["client_order_id"],
        broker_order_id=row["broker_order_id"],
        symbol=row["symbol"],
        side=row["side"],
        qty=row["qty"],
        type=row["type"],
        entry_price=row["entry_price"],
        stop_price=row["stop_price"],
        take_profit_price=row["take_profit_price"],
        status=row["status"],
        submitted_at=row["submitted_at"],
        raw_response=row["raw_response"],
    )


def update_status(
    db: DbConnection,
    *,
    client_order_id: str,
    status: str,
    broker_order_id: str | None = None,
) -> None:
    if broker_order_id is not None:
        db.execute(
            "UPDATE orders SET status = ?, broker_order_id = COALESCE(broker_order_id, ?) WHERE client_order_id = ?",
            (status, broker_order_id, client_order_id),
        )
    else:
        db.execute(
            "UPDATE orders SET status = ? WHERE client_order_id = ?",
            (status, client_order_id),
        )
    db.commit()
