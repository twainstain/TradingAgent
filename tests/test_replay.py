"""Smoke test for scripts/replay.py — no live Alpaca needed (uses seeded bars)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def db_path(tmp_path):
    from trading_platform.persistence.db import close_db, init_db

    schema = (Path(__file__).resolve().parent.parent / "src" / "schema.sql").read_text()
    p = tmp_path / "events.db"
    conn = init_db(p, schema)
    # Seed 260 trading days of bars for AAPL & MSFT.
    from storage.bars_repo import upsert_bars

    for sym in ("AAPL", "MSFT"):
        rng = np.random.default_rng(hash(sym) & 0xFFFF_FFFF)
        n = 260
        returns = rng.normal(0.0003, 0.012, n)
        close = 100.0 * np.exp(np.cumsum(returns))
        high = close * (1 + np.abs(rng.normal(0, 0.004, n)))
        low = close * (1 - np.abs(rng.normal(0, 0.004, n)))
        open_ = close * (1 + rng.normal(0, 0.002, n))
        vol = rng.integers(1_000_000, 10_000_000, n).astype(int)
        dates = pd.date_range("2025-01-01", periods=n, freq="B")
        rows = [
            (sym, d.date().isoformat(), float(o), float(h), float(l), float(c), int(v))
            for d, o, h, l, c, v in zip(dates, open_, high, low, close, vol)
        ]
        upsert_bars(conn, rows)
    close_db()
    return p


def test_replay_runs_and_reports(db_path, capsys) -> None:
    import scripts.replay as replay

    target = "2025-12-31"  # some date in the seeded range
    rc = replay.main(["--date", target, "--db", str(db_path), "--symbols", "AAPL,MSFT"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AAPL" in out
    assert "MSFT" in out
    assert "replay" in out and "complete" in out


def test_replay_handles_missing_date(db_path, capsys) -> None:
    import scripts.replay as replay

    rc = replay.main(["--date", "2099-01-01", "--db", str(db_path), "--symbols", "AAPL"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no bar" in out or "no bars" in out
