"""bars_daily repo + fetch-to-DB round trip using a fake Alpaca client."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

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


def test_upsert_and_load(db) -> None:
    from storage.bars_repo import load_bars, upsert_bars

    rows = [
        ("AAPL", "2026-01-02", 180.0, 182.0, 179.5, 181.5, 80_000_000),
        ("AAPL", "2026-01-03", 181.5, 183.0, 181.0, 182.5, 75_000_000),
        ("MSFT", "2026-01-03", 420.0, 425.0, 418.0, 423.0, 30_000_000),
    ]
    written = upsert_bars(db, rows)
    assert written == 3

    df = load_bars(db, "AAPL")
    assert len(df) == 2
    assert df["close"].iloc[-1] == 182.5
    assert df.index.is_monotonic_increasing

    assert load_bars(db, "GOOGL").empty


def test_upsert_overwrites_same_key(db) -> None:
    from storage.bars_repo import load_bars, upsert_bar

    upsert_bar(db, "AAPL", "2026-01-02", 180, 181, 179, 180.5, 1_000)
    upsert_bar(db, "AAPL", "2026-01-02", 180, 181, 179, 999.0, 9_999)  # replace
    db.commit()
    df = load_bars(db, "AAPL")
    assert df["close"].iloc[-1] == 999.0
    assert df["volume"].iloc[-1] == 9_999


def test_fetch_daily_bars_with_fake_client(db) -> None:
    from storage.bars_repo import load_bars, upsert_bars
    from market.alpaca_rest import fetch_daily_bars

    # Build a minimal fake Alpaca response shape: resp.data = {sym: [bar, ...]}
    class _Bar:
        def __init__(self, t, o, h, l, c, v):
            self.timestamp = t
            self.open = o
            self.high = h
            self.low = l
            self.close = c
            self.volume = v

    fake_resp = SimpleNamespace(
        data={
            "AAPL": [
                _Bar(datetime(2026, 1, 2, tzinfo=timezone.utc), 180, 182, 179, 181, 80_000_000),
                _Bar(datetime(2026, 1, 3, tzinfo=timezone.utc), 181, 183, 181, 182, 75_000_000),
            ]
        }
    )

    class _FakeClient:
        def get_stock_bars(self, req):
            return fake_resp

    rows = fetch_daily_bars(["AAPL"], lookback_days=60, client=_FakeClient())
    assert len(rows) == 2
    assert rows[0][0] == "AAPL"
    assert rows[0][1] == "2026-01-02"
    upsert_bars(db, rows)

    df = load_bars(db, "AAPL")
    assert len(df) == 2
    assert df["close"].iloc[-1] == 182.0
