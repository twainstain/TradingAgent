"""Phase 1: snapshot write/read round-trip + 90s freshness filter + retention."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def db(tmp_path):
    from trading_platform.persistence.db import init_db, close_db

    schema = (Path(__file__).resolve().parent.parent / "src" / "schema.sql").read_text()
    conn = init_db(tmp_path / "events.db", schema)
    try:
        yield conn
    finally:
        close_db()


def _make_row(symbol: str, ts: datetime, **overrides):
    from storage.snapshot_repo import SnapshotRow

    base = dict(
        symbol=symbol,
        ts=ts,
        price=100.0,
        rsi14=55.0,
        sma20=99.0,
        sma50=98.0,
        sma200=95.0,
        avg_vol_20=3_500_000.0,
        atr14=1.25,
        price_vs_sma50_pct=2.04,
        tick_id=None,
    )
    base.update(overrides)
    return SnapshotRow(**base)


def test_write_read_roundtrip(db) -> None:
    from storage.snapshot_repo import latest_snapshot, write_snapshot

    now = datetime.now(timezone.utc)
    row = _make_row("AAPL", now)
    write_snapshot(db, row)

    got = latest_snapshot(db, "AAPL", now=now)
    assert got is not None
    assert got.symbol == "AAPL"
    assert got.price == 100.0
    assert got.rsi14 == 55.0
    # ts should round-trip to (approximately) the same instant
    assert abs((got.ts - now).total_seconds()) < 1


def test_nan_values_stored_as_null(db) -> None:
    from storage.snapshot_repo import latest_snapshot, write_snapshot

    now = datetime.now(timezone.utc)
    row = _make_row("AAPL", now, sma200=float("nan"))
    write_snapshot(db, row)
    got = latest_snapshot(db, "AAPL", now=now)
    assert got is not None
    assert got.sma200 is None


def test_freshness_filter_rejects_stale(db) -> None:
    from storage.snapshot_repo import latest_snapshot, write_snapshot

    now = datetime.now(timezone.utc)
    stale_ts = now - timedelta(seconds=120)  # older than 90s default
    write_snapshot(db, _make_row("AAPL", stale_ts))

    assert latest_snapshot(db, "AAPL", now=now) is None
    assert latest_snapshot(db, "AAPL", freshness_seconds=300, now=now) is not None


def test_freshness_returns_newest_within_window(db) -> None:
    from storage.snapshot_repo import latest_snapshot, write_snapshot

    now = datetime.now(timezone.utc)
    write_snapshot(db, _make_row("AAPL", now - timedelta(seconds=60), price=101.0))
    write_snapshot(db, _make_row("AAPL", now - timedelta(seconds=30), price=102.0))
    write_snapshot(db, _make_row("AAPL", now - timedelta(seconds=10), price=103.0))

    got = latest_snapshot(db, "AAPL", now=now)
    assert got is not None
    assert got.price == 103.0


def test_latest_snapshots_bulk(db) -> None:
    from storage.snapshot_repo import latest_snapshots, write_snapshot

    now = datetime.now(timezone.utc)
    write_snapshot(db, _make_row("AAPL", now, price=100.0))
    write_snapshot(db, _make_row("MSFT", now, price=200.0))
    # Stale row should be excluded
    write_snapshot(db, _make_row("NVDA", now - timedelta(seconds=300), price=300.0))

    out = latest_snapshots(db, ["AAPL", "MSFT", "NVDA"], now=now)
    assert set(out.keys()) == {"AAPL", "MSFT"}
    assert out["AAPL"].price == 100.0
    assert out["MSFT"].price == 200.0


def test_prune_older_than(db) -> None:
    from storage.snapshot_repo import prune_older_than, write_snapshot

    now = datetime.now(timezone.utc)
    write_snapshot(db, _make_row("AAPL", now - timedelta(days=3), price=1.0))
    write_snapshot(db, _make_row("AAPL", now - timedelta(days=1), price=2.0))
    write_snapshot(db, _make_row("AAPL", now, price=3.0))

    cutoff = now - timedelta(days=2)
    deleted = prune_older_than(db, cutoff)
    assert deleted == 1

    remaining = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert remaining == 2
