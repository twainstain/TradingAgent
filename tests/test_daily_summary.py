"""Phase 5: daily summary markdown output."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path

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


def _seed(db, td: date):
    iso = datetime.combine(td, time(12, 0, tzinfo=timezone.utc)).isoformat()
    # A tick
    db.execute("INSERT INTO ticks (started_at, status) VALUES (?, 'ok')", (iso,))
    db.commit()
    tid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    # A signal (rule_only)
    db.execute("""INSERT INTO signals (tick_id, symbol, strategy, side, confidence, reason, llm_branch, created_at)
                  VALUES (?, 'AAPL', 'mean_reversion', 'buy', 1, 'x', 'rule_only', ?)""", (tid, iso))
    db.commit()
    sid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("""INSERT INTO risk_decisions (signal_id, approved, reason, sized_qty, created_at)
                  VALUES (?, 1, 'ok', 10, ?)""", (sid, iso))
    db.commit()
    rdid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    # One bracket order + filled
    db.execute("""INSERT INTO orders (risk_decision_id, client_order_id, broker_order_id, symbol, side,
                                      qty, type, entry_price, stop_price, take_profit_price, status, submitted_at)
                  VALUES (?, ?, 'b-1', 'AAPL', 'buy', 10, 'bracket', 180.0, 176.4, 187.2, 'filled', ?)""",
               (rdid, f"{tid}:AAPL", iso))
    db.commit()
    oid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("""INSERT INTO fills (order_id, client_order_id, filled_qty, filled_avg_price, status, reported_at)
                  VALUES (?, ?, 10, 180.05, 'filled', ?)""", (oid, f"{tid}:AAPL", iso))
    db.commit()
    # LLM call
    db.execute("""INSERT INTO llm_calls (model, prompt_hash, prompt, response, tokens_in, tokens_out,
                                         cost_usd, cache_hit, latency_ms, created_at)
                  VALUES ('claude-haiku-4-5-20251001', 'h', 'p', 'r', 100, 50, 0.0013, 1, 450, ?)""", (iso,))
    db.commit()


def test_daily_summary_renders_and_writes(tmp_path, db) -> None:
    from orchestrator.daily_summary import render, write_summary

    td = date(2026, 4, 22)
    _seed(db, td)

    md = render(db, td)
    assert "# Daily Summary — 2026-04-22" in md
    assert "Filled orders: **1**" in md
    assert "rule_only" in md
    assert "$0.0013" in md

    out = write_summary(db, td, log_dir=tmp_path)
    assert out.path.exists()
    assert out.path.name == "daily_summary_2026-04-22.md"


def test_daily_summary_latency_percentiles(tmp_path, db) -> None:
    from orchestrator.daily_summary import render

    td = date(2026, 4, 22)
    _seed(db, td)
    latency_path = tmp_path / "latency.jsonl"
    ts = datetime.combine(td, time(12, 0, tzinfo=timezone.utc)).isoformat()
    # Stage marks: indicators_ms varies, orders_sent_ms stable
    for v in (1.0, 2.0, 3.0, 4.0, 5.0, 20.0):
        latency_path.open("a").write(json.dumps({
            "timestamp": ts,
            "cycle_marks": {"indicators_ms": v, "orders_sent_ms": 10.0},
        }) + "\n")

    md = render(db, td, latency_path=latency_path)
    assert "indicators_ms" in md
    assert "orders_sent_ms" in md
    assert "p95=" in md
