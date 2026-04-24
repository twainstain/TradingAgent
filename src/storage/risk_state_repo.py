"""risk_state table — sticky per-day halt flag.

Survives process restart (CLAUDE.md invariant #7). Keyed by trading_date
(ET); only the date rollover clears it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from trading_platform.persistence.db import DbConnection


@dataclass(frozen=True)
class RiskState:
    trading_date: str  # YYYY-MM-DD
    halted: bool
    engaged_at: str | None
    reason: str | None
    starting_equity: Decimal | None
    current_pnl: Decimal | None
    kill_switch_engaged: bool


def _to_trading_date(d: date | str) -> str:
    if isinstance(d, str):
        return d[:10]
    return d.isoformat()


def _row_to_state(r: Any) -> RiskState:
    return RiskState(
        trading_date=r["trading_date"],
        halted=bool(r["halted"]),
        engaged_at=r["engaged_at"],
        reason=r["reason"],
        starting_equity=Decimal(str(r["starting_equity"])) if r["starting_equity"] is not None else None,
        current_pnl=Decimal(str(r["current_pnl"])) if r["current_pnl"] is not None else None,
        kill_switch_engaged=bool(r["kill_switch_engaged"]),
    )


def get_state(db: DbConnection, trading_date: date | str) -> RiskState | None:
    row = db.execute(
        "SELECT * FROM risk_state WHERE trading_date = ?",
        (_to_trading_date(trading_date),),
    ).fetchone()
    return _row_to_state(row) if row else None


def ensure_state(
    db: DbConnection,
    trading_date: date | str,
    *,
    starting_equity: Decimal | None = None,
) -> RiskState:
    """Idempotently create (or return existing) row for the given trading date."""
    existing = get_state(db, trading_date)
    if existing is not None:
        return existing
    td = _to_trading_date(trading_date)
    se = float(starting_equity) if starting_equity is not None else None
    db.execute(
        """
        INSERT INTO risk_state (trading_date, halted, engaged_at, reason,
                                starting_equity, current_pnl, kill_switch_engaged)
        VALUES (?, 0, NULL, NULL, ?, NULL, 0)
        """,
        (td, se),
    )
    db.commit()
    return get_state(db, trading_date)  # type: ignore[return-value]


def mark_halted(
    db: DbConnection,
    trading_date: date | str,
    reason: str,
    *,
    engaged_at: datetime | None = None,
    current_pnl: Decimal | None = None,
    starting_equity: Decimal | None = None,
) -> RiskState:
    ensure_state(db, trading_date, starting_equity=starting_equity)
    eng_at = (engaged_at or datetime.now(timezone.utc)).isoformat()
    cp = float(current_pnl) if current_pnl is not None else None
    db.execute(
        """
        UPDATE risk_state
           SET halted = 1,
               engaged_at = COALESCE(engaged_at, ?),
               reason = COALESCE(reason, ?),
               current_pnl = ?
         WHERE trading_date = ?
        """,
        (eng_at, reason, cp, _to_trading_date(trading_date)),
    )
    db.commit()
    return get_state(db, trading_date)  # type: ignore[return-value]


def mark_kill_switch_engaged(db: DbConnection, trading_date: date | str) -> None:
    ensure_state(db, trading_date)
    db.execute(
        "UPDATE risk_state SET kill_switch_engaged = 1 WHERE trading_date = ?",
        (_to_trading_date(trading_date),),
    )
    db.commit()


def is_halted_today(db: DbConnection, trading_date: date | str) -> bool:
    st = get_state(db, trading_date)
    return bool(st and st.halted)
