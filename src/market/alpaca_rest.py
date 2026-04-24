"""Alpaca REST — 60-day daily-bar backfill on startup.

Only surface used in Phase 1. Fills `bars_daily` so `indicators.compute_features`
has enough history for SMA-200 (well — it needs 200 trading days; 60 calendar
days is what the plan specifies for the minimum viable start-up snapshot. The
retention job keeps `snapshots` small; `bars_daily` grows unbounded and is
what feeds long-window indicators).

We do *not* compute indicators here — that's the data agent's job each tick.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable

from trading_platform.persistence.db import DbConnection

from storage.bars_repo import upsert_bars

log = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 60


def _client():
    """Build an Alpaca historical-data client from env. Imported lazily so tests
    that don't touch this path don't require alpaca-py to be installed."""
    from alpaca.data.historical import StockHistoricalDataClient

    return StockHistoricalDataClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_API_SECRET"],
    )


def fetch_daily_bars(
    symbols: Iterable[str],
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    end: datetime | None = None,
    client=None,
) -> list[tuple]:
    """Fetch daily bars from Alpaca. Returns rows ready for `upsert_bars`.

    Rows: (symbol, date_str, open, high, low, close, volume).
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    if client is None:
        client = _client()

    end_dt = end or datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)

    req = StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=TimeFrame.Day,
        start=start_dt,
        end=end_dt,
    )
    resp = client.get_stock_bars(req)
    # alpaca-py returns a BarSet with .data: dict[symbol, list[Bar]]
    rows: list[tuple] = []
    data = getattr(resp, "data", resp)
    for sym, bars in data.items():
        for b in bars:
            ts = b.timestamp
            date_str = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
            rows.append((sym, date_str, float(b.open), float(b.high), float(b.low), float(b.close), int(b.volume)))
    return rows


def backfill_bars_daily(
    db: DbConnection,
    symbols: Iterable[str],
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> int:
    """One-shot startup backfill. Returns rows written."""
    rows = fetch_daily_bars(symbols, lookback_days=lookback_days)
    written = upsert_bars(db, rows)
    log.info("backfilled %d bars across %d symbols", written, len(list(symbols)))
    return written
