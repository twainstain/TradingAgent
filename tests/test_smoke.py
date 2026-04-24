"""Phase 0 smoke tests: sys.path is wired, dashboard /health works, schema parses."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def test_src_on_syspath() -> None:
    assert any(p.endswith("/src") for p in sys.path), "conftest.py must prepend src/ to sys.path"


def test_schema_creates_all_tables(tmp_path) -> None:
    schema = (Path(__file__).resolve().parent.parent / "src" / "schema.sql").read_text()
    db_path = tmp_path / "events.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()

    tables = {r[0] for r in rows}
    expected = {
        "ticks",
        "bars_daily",
        "snapshots",
        "signals",
        "llm_calls",
        "risk_decisions",
        "orders",
        "fills",
        "risk_state",
        "latency_traces",
    }
    missing = expected - tables
    assert not missing, f"schema.sql is missing tables: {missing}"


def test_dashboard_health() -> None:
    from fastapi.testclient import TestClient

    from dashboard.app import app

    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
