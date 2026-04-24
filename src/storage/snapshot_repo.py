"""Snapshot persistence — thin wrapper over the shared DbConnection.

Freshness is a read-time filter, NOT a stored column (per ARCHITECTURE §3.1
and CLAUDE.md). Rows older than the freshness window are simply not returned
to callers that ask for the "latest usable" snapshot.

Retention is handled by `prune_older_than`, which the data agent calls once
per tick to keep `snapshots` bounded (~2 trading days).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from trading_platform.persistence.db import DbConnection

DEFAULT_FRESHNESS_SECONDS = 90


@dataclass(frozen=True)
class SnapshotRow:
    symbol: str
    ts: datetime
    price: float
    rsi14: float | None
    sma20: float | None
    sma50: float | None
    sma200: float | None
    avg_vol_20: float | None
    atr14: float | None
    price_vs_sma50_pct: float | None
    tick_id: int | None = None


def _to_iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _from_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _nan_to_none(x: Any) -> float | None:
    if x is None:
        return None
    try:
        if isinstance(x, float) and math.isnan(x):
            return None
    except TypeError:
        pass
    return float(x)


def write_snapshot(db: DbConnection, row: SnapshotRow) -> None:
    db.execute(
        """
        INSERT INTO snapshots
            (tick_id, symbol, ts, price, rsi14, sma20, sma50, sma200,
             avg_vol_20, atr14, price_vs_sma50_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.tick_id,
            row.symbol,
            _to_iso(row.ts),
            float(row.price),
            _nan_to_none(row.rsi14),
            _nan_to_none(row.sma20),
            _nan_to_none(row.sma50),
            _nan_to_none(row.sma200),
            _nan_to_none(row.avg_vol_20),
            _nan_to_none(row.atr14),
            _nan_to_none(row.price_vs_sma50_pct),
        ),
    )
    db.commit()


def write_snapshots(db: DbConnection, rows: Iterable[SnapshotRow]) -> int:
    n = 0
    with db.batch():
        for row in rows:
            write_snapshot(db, row)
            n += 1
    return n


def _row_to_obj(r: Any) -> SnapshotRow:
    # sqlite3.Row supports both index and column access.
    return SnapshotRow(
        tick_id=r["tick_id"],
        symbol=r["symbol"],
        ts=_from_iso(r["ts"]),
        price=r["price"],
        rsi14=r["rsi14"],
        sma20=r["sma20"],
        sma50=r["sma50"],
        sma200=r["sma200"],
        avg_vol_20=r["avg_vol_20"],
        atr14=r["atr14"],
        price_vs_sma50_pct=r["price_vs_sma50_pct"],
    )


def latest_snapshot(
    db: DbConnection,
    symbol: str,
    *,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
    now: datetime | None = None,
) -> SnapshotRow | None:
    """Return the most recent snapshot within the freshness window, else None."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=freshness_seconds)
    cur = db.execute(
        """
        SELECT * FROM snapshots
        WHERE symbol = ? AND ts >= ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (symbol, _to_iso(cutoff)),
    )
    row = cur.fetchone()
    return _row_to_obj(row) if row else None


def latest_snapshots(
    db: DbConnection,
    symbols: Iterable[str],
    *,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
    now: datetime | None = None,
) -> dict[str, SnapshotRow]:
    """Bulk-fetch latest non-stale snapshots keyed by symbol."""
    out: dict[str, SnapshotRow] = {}
    for sym in symbols:
        hit = latest_snapshot(db, sym, freshness_seconds=freshness_seconds, now=now)
        if hit is not None:
            out[sym] = hit
    return out


def prune_older_than(db: DbConnection, cutoff: datetime) -> int:
    """Delete snapshot rows older than `cutoff`. Returns rows deleted."""
    cur = db.execute("DELETE FROM snapshots WHERE ts < ?", (_to_iso(cutoff),))
    # sqlite3.Cursor exposes rowcount after DELETE
    deleted = getattr(cur, "rowcount", 0) or 0
    db.commit()
    return int(deleted)
