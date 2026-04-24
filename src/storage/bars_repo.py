"""bars_daily repository — write/read daily OHLCV bars."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import pandas as pd

from trading_platform.persistence.db import DbConnection


def _iso_date(d: date | datetime | str) -> str:
    if isinstance(d, str):
        return d[:10]
    if isinstance(d, datetime):
        return d.astimezone(timezone.utc).date().isoformat()
    return d.isoformat()


def upsert_bar(
    db: DbConnection,
    symbol: str,
    bar_date: date | datetime | str,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: int,
) -> None:
    db.execute(
        """
        INSERT OR REPLACE INTO bars_daily
            (symbol, date, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (symbol, _iso_date(bar_date), float(open_), float(high), float(low), float(close), int(volume)),
    )


def upsert_bars(db: DbConnection, bars: Iterable[tuple]) -> int:
    """bars rows: (symbol, date, open, high, low, close, volume)."""
    n = 0
    with db.batch():
        for sym, d, o, h, l, c, v in bars:
            upsert_bar(db, sym, d, o, h, l, c, v)
            n += 1
    db.commit()
    return n


def load_bars(
    db: DbConnection,
    symbol: str,
    *,
    lookback_days: int | None = None,
) -> pd.DataFrame:
    """Return bars for `symbol` as an ascending-date DataFrame with a DatetimeIndex."""
    if lookback_days is not None:
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=lookback_days)).isoformat()
        cur = db.execute(
            """
            SELECT date, open, high, low, close, volume FROM bars_daily
            WHERE symbol = ? AND date >= ?
            ORDER BY date ASC
            """,
            (symbol, cutoff),
        )
    else:
        cur = db.execute(
            """
            SELECT date, open, high, low, close, volume FROM bars_daily
            WHERE symbol = ?
            ORDER BY date ASC
            """,
            (symbol,),
        )
    rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    dates = [pd.Timestamp(r["date"]) for r in rows]
    data = {
        "open": [r["open"] for r in rows],
        "high": [r["high"] for r in rows],
        "low": [r["low"] for r in rows],
        "close": [r["close"] for r in rows],
        "volume": [r["volume"] for r in rows],
    }
    return pd.DataFrame(data, index=pd.DatetimeIndex(dates, name="date"))
