"""signals table persistence."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from trading_platform.persistence.db import DbConnection

from core.signal import Signal


def _to_iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def write_signal(db: DbConnection, signal: Signal) -> int:
    """Insert a signal row. `tick_id` must be set (FK NOT NULL)."""
    if signal.tick_id is None:
        raise ValueError("signal.tick_id is required — the orchestrator must create a ticks row first")
    cur = db.execute(
        """
        INSERT INTO signals
            (tick_id, symbol, strategy, side, confidence, reason,
             llm_call_id, llm_branch, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal.tick_id,
            signal.symbol,
            signal.strategy,
            signal.side,
            float(signal.confidence),
            signal.reason,
            signal.llm_call_id,
            signal.llm_branch,
            _to_iso(signal.created_at),
        ),
    )
    db.commit()
    return int(cur.lastrowid)


def write_signals(db: DbConnection, signals: Iterable[Signal]) -> list[int]:
    ids: list[int] = []
    with db.batch():
        for s in signals:
            ids.append(write_signal(db, s))
    return ids


def signals_for_tick(db: DbConnection, tick_id: int) -> list[Signal]:
    cur = db.execute(
        """
        SELECT symbol, strategy, side, confidence, reason, created_at,
               tick_id, llm_call_id, llm_branch
        FROM signals
        WHERE tick_id = ?
        ORDER BY id ASC
        """,
        (tick_id,),
    )
    rows = cur.fetchall()
    return [
        Signal(
            symbol=r["symbol"],
            side=r["side"],
            strategy=r["strategy"],
            confidence=r["confidence"] or 0.0,
            reason=r["reason"] or "",
            created_at=datetime.fromisoformat(r["created_at"]),
            tick_id=r["tick_id"],
            llm_call_id=r["llm_call_id"],
            llm_branch=r["llm_branch"],
        )
        for r in rows
    ]
