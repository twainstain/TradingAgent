"""Phase 5b: dashboard views + RO DB + localhost-only kill-switch."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Create a writable events.db with seed data, then point the dashboard env at it.

    Dashboard opens the file read-only; the fixture writes through a normal
    connection once up front.
    """
    schema = (Path(__file__).resolve().parent.parent / "src" / "schema.sql").read_text()
    db_path = tmp_path / "events.db"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    kill = tmp_path / "KILL"
    latency = log_dir / "latency.jsonl"

    # Seed: ticks, signal, risk_decision, order, fill, risk_state, latency line, daily summary.
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    td = date(2026, 4, 22)
    iso = datetime.combine(td, time(12, 0, tzinfo=timezone.utc)).isoformat()
    conn.execute("INSERT INTO ticks (started_at, status) VALUES (?, 'ok')", (iso,))
    tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO signals (tick_id, symbol, strategy, side, confidence, reason, llm_branch, created_at)
           VALUES (?, 'AAPL', 'mean_reversion', 'buy', 1, 'oversold bounce', 'rule_only', ?)""",
        (tid, iso),
    )
    sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO risk_decisions (signal_id, approved, reason, sized_qty, created_at)
           VALUES (?, 1, 'all_rules_passed', 20, ?)""",
        (sid, iso),
    )
    rdid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO orders (risk_decision_id, client_order_id, broker_order_id, symbol, side,
                               qty, type, entry_price, stop_price, take_profit_price, status, submitted_at)
           VALUES (?, ?, 'b-1', 'AAPL', 'buy', 20, 'bracket', 150.0, 147.0, 156.0, 'filled', ?)""",
        (rdid, f"{tid}:AAPL", iso),
    )
    oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO fills (order_id, client_order_id, filled_qty, filled_avg_price, status, reported_at)
           VALUES (?, ?, 20, 150.05, 'filled', ?)""",
        (oid, f"{tid}:AAPL", iso),
    )
    conn.execute(
        """INSERT INTO risk_state (trading_date, halted, engaged_at, reason, starting_equity,
                                   current_pnl, kill_switch_engaged)
           VALUES (?, 0, NULL, NULL, 100000.0, 0.0, 0)""",
        (td.isoformat(),),
    )
    conn.commit()
    conn.close()

    # Latency line.
    latency.write_text(
        json.dumps({"timestamp": iso, "cycle_marks": {"indicators_ms": 3.2, "orders_sent_ms": 1.0}}) + "\n"
    )
    # Daily summary file.
    (log_dir / f"daily_summary_{td.isoformat()}.md").write_text("# Daily Summary — test\n- ok\n")

    monkeypatch.setenv("EVENTS_DB", str(db_path))
    monkeypatch.setenv("KILL_SWITCH_PATH", str(kill))
    monkeypatch.setenv("LATENCY_PATH", str(latency))
    monkeypatch.setenv("LOG_DIR", str(log_dir))

    yield {
        "db_path": db_path,
        "kill_path": kill,
        "latency_path": latency,
        "log_dir": log_dir,
        "trading_date": td,
    }


@pytest.fixture
def client(env):
    # TestClient defaults `client=("testclient", 50000)`, which would fail the
    # loopback check on /admin/kill-switch/engage. Force a loopback origin.
    from fastapi.testclient import TestClient

    from dashboard.app import app

    return TestClient(app, client=("127.0.0.1", 12345))


# ---------------------------------------------------------------------------
# View rendering
# ---------------------------------------------------------------------------

def test_health(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_today_view_renders(client, env) -> None:
    # Fix "today" to match our seed data by navigating with the date in the URL isn't supported on /,
    # so we check with a fresh seed at today's date.
    # Simpler: reseed at today's date in a scratch DB via Signals page with explicit date.
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Today" in html
    assert "clear" in html or "not engaged" in html  # one of the status badges


def test_signals_view_filters(client, env) -> None:
    td = env["trading_date"].isoformat()
    r = client.get(f"/signals?date={td}")
    assert r.status_code == 200
    html = r.text
    assert "AAPL" in html
    assert "rule_only" in html
    assert "approved" in html  # risk decision rendered

    r2 = client.get(f"/signals?date={td}&symbol=MSFT")
    assert r2.status_code == 200
    # "AAPL" also appears in the filter form placeholder — check the results header instead.
    assert "0 signals" in r2.text


def test_latency_view(client, env) -> None:
    td = env["trading_date"].isoformat()
    r = client.get(f"/latency?date={td}")
    assert r.status_code == 200
    assert "indicators_ms" in r.text
    assert "orders_sent_ms" in r.text


def test_daily_view_renders_markdown(client, env) -> None:
    td = env["trading_date"].isoformat()
    r = client.get(f"/daily?date={td}")
    assert r.status_code == 200
    assert "Daily Summary" in r.text


def test_admin_view_lists_risk_state(client, env) -> None:
    r = client.get("/admin")
    assert r.status_code == 200
    assert env["trading_date"].isoformat() in r.text


def test_bad_date_returns_400(client) -> None:
    r = client.get("/signals?date=not-a-date")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Read-only DB invariant
# ---------------------------------------------------------------------------

def test_db_is_opened_read_only(env) -> None:
    from dashboard.db import open_readonly

    conn = open_readonly(env["db_path"])
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO ticks (started_at, status) VALUES ('x', 'x')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM ticks")
        # SELECT still works.
        rows = conn.execute("SELECT COUNT(*) FROM ticks").fetchone()
        assert rows[0] >= 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Kill-switch endpoint
# ---------------------------------------------------------------------------

def test_kill_switch_engage_from_localhost(client, env) -> None:
    assert not env["kill_path"].exists()
    r = client.post("/admin/kill-switch/engage", data={"confirm": "ENGAGE"}, follow_redirects=False)
    assert r.status_code == 303
    assert env["kill_path"].exists()


def test_kill_switch_requires_confirm_text(client, env) -> None:
    r = client.post("/admin/kill-switch/engage", data={"confirm": "whatever"}, follow_redirects=False)
    assert r.status_code == 400
    assert not env["kill_path"].exists()


def test_kill_switch_refuses_non_loopback(env, monkeypatch) -> None:
    """Spoof request.client.host to a non-loopback IP and verify a 403."""
    from starlette.requests import Request

    from dashboard.app import engage_kill_switch

    async def _fake_form():
        return {"confirm": "ENGAGE"}

    class _Client:
        host = "203.0.113.7"

    class _Req:
        client = _Client()
        async def form(self):
            return await _fake_form()

    import asyncio

    from fastapi import HTTPException

    async def run():
        try:
            await engage_kill_switch(_Req())  # type: ignore[arg-type]
            return None
        except HTTPException as exc:
            return exc

    exc = asyncio.get_event_loop().run_until_complete(run())
    assert exc is not None and exc.status_code == 403
    assert not env["kill_path"].exists()
