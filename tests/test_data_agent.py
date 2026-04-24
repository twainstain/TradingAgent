"""Data Agent integration: bars → indicators → snapshot + cache + retention."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def db(tmp_path):
    from trading_platform.persistence.db import close_db, init_db

    schema = (Path(__file__).resolve().parent.parent / "src" / "schema.sql").read_text()
    conn = init_db(tmp_path / "events.db", schema)
    try:
        yield conn
    finally:
        close_db()


def _seed_bars(db, symbol: str, n: int = 260) -> None:
    from storage.bars_repo import upsert_bars

    rng = np.random.default_rng(hash(symbol) & 0xFFFF_FFFF)
    returns = rng.normal(0.0003, 0.012, n)
    close = 100.0 * np.exp(np.cumsum(returns))
    high = close * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, n)))
    open_ = close * (1 + rng.normal(0, 0.002, n))
    vol = rng.integers(1_000_000, 10_000_000, n).astype(int)
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    rows = [
        (symbol, d.date().isoformat(), float(o), float(h), float(l), float(c), int(v))
        for d, o, h, l, c, v in zip(dates, open_, high, low, close, vol)
    ]
    upsert_bars(db, rows)


def test_run_tick_writes_snapshots(db) -> None:
    from market.data_agent import DataAgent

    _seed_bars(db, "AAPL")
    _seed_bars(db, "MSFT")

    # Simulate the orchestrator inserting a ticks row before the data agent runs.
    now = datetime.now(timezone.utc)
    db.execute("INSERT INTO ticks (started_at, status) VALUES (?, 'running')", (now.isoformat(),))
    db.commit()
    tick_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    agent = DataAgent(db, symbols=("AAPL", "MSFT"))
    rows = agent.run_tick({"AAPL": 150.0, "MSFT": 420.0}, tick_id=tick_id, now=now)
    assert len(rows) == 2
    assert {r.symbol for r in rows} == {"AAPL", "MSFT"}

    cur = db.execute("SELECT COUNT(*) FROM snapshots")
    assert cur.fetchone()[0] == 2

    assert agent.cache.has("AAPL")


def test_missing_price_is_skipped(db) -> None:
    from market.data_agent import DataAgent

    _seed_bars(db, "AAPL")
    agent = DataAgent(db, symbols=("AAPL", "MSFT"))
    rows = agent.run_tick({"AAPL": 150.0})  # MSFT price absent
    assert [r.symbol for r in rows] == ["AAPL"]


def test_no_bars_symbol_is_skipped(db) -> None:
    from market.data_agent import DataAgent

    _seed_bars(db, "AAPL")
    agent = DataAgent(db, symbols=("AAPL", "TSLA"))
    rows = agent.run_tick({"AAPL": 150.0, "TSLA": 250.0})
    assert [r.symbol for r in rows] == ["AAPL"]


def test_retention_prunes_old_rows(db) -> None:
    from datetime import timezone as tz

    from market.data_agent import DataAgent
    from storage.snapshot_repo import SnapshotRow, write_snapshot

    _seed_bars(db, "AAPL")
    now = datetime.now(tz.utc)
    old_row = SnapshotRow(
        symbol="AAPL",
        ts=now - timedelta(days=5),
        price=100.0,
        rsi14=50.0, sma20=99.0, sma50=98.0, sma200=95.0,
        avg_vol_20=3_000_000.0, atr14=1.0, price_vs_sma50_pct=2.0,
    )
    write_snapshot(db, old_row)

    agent = DataAgent(db, symbols=("AAPL",), retention_days=2)
    agent.run_tick({"AAPL": 150.0}, now=now)

    cur = db.execute("SELECT COUNT(*) FROM snapshots WHERE ts < ?", (
        (now - timedelta(days=2)).isoformat(),
    ))
    assert cur.fetchone()[0] == 0


def test_latency_tracker_writes_marks(db, tmp_path) -> None:
    from market.data_agent import DataAgent
    from trading_platform.observability import LatencyTracker

    _seed_bars(db, "AAPL")
    log_path = tmp_path / "latency.jsonl"
    tracker = LatencyTracker(str(log_path))

    agent = DataAgent(db, symbols=("AAPL",), latency_tracker=tracker)
    agent.run_tick({"AAPL": 150.0})

    marks = tracker.get_marks()
    assert "indicators_ms" in marks
    assert "snapshot_write_ms" in marks
    assert "retention_ms" in marks
    # each mark should be a non-negative float
    assert all(isinstance(v, (int, float)) and v >= 0 for v in marks.values())


def test_record_quote_roundtrip(db) -> None:
    from market.data_agent import DataAgent

    _seed_bars(db, "AAPL")
    agent = DataAgent(db, symbols=("AAPL", "MSFT"))
    agent.record_quote("aapl", 175.5)
    agent.record_quote("MSFT", 430.0)

    pm = agent.price_map()
    assert pm == {"AAPL": 175.5, "MSFT": 430.0}
    assert agent.latest_price("AAPL") == 175.5
    assert agent.latest_price("UNKNOWN") is None
