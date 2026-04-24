"""Phase 5: LLM cost tracker + cap trigger (exit criterion)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
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


def _seed_call(db, ts_iso: str, cost: float) -> None:
    db.execute(
        """
        INSERT INTO llm_calls (model, prompt_hash, prompt, response, tokens_in, tokens_out,
                               cost_usd, cache_hit, latency_ms, created_at)
        VALUES ('claude-haiku-4-5-20251001', 'abc', 'p', 'r', 100, 50, ?, 0, 10, ?)
        """,
        (cost, ts_iso),
    )
    db.commit()


def test_estimate_cost_haiku_defaults() -> None:
    from llm.cost_tracker import estimate_cost

    # 1M in tokens = $1, 1M out tokens = $5.
    est = estimate_cost(1_000_000, 1_000_000)
    assert est.cost_usd == Decimal("6.00")


def test_estimate_cost_cached_input_discount() -> None:
    from llm.cost_tracker import estimate_cost

    est_cached = estimate_cost(1_000_000, 0, cache_hit=True)
    assert est_cached.cost_usd == Decimal("0.10")


def test_cap_triggers_at_or_above_threshold(db) -> None:
    from llm.cost_tracker import DailyCostTracker

    td = date(2026, 4, 22)
    ts = datetime.combine(td, datetime.min.time().replace(hour=10), tzinfo=timezone.utc).isoformat()
    _seed_call(db, ts, cost=5.00)  # exactly at cap
    tracker = DailyCostTracker(db, cap_usd=Decimal("5"))
    assert tracker.is_over_cap(td) is True


def test_cap_under_threshold_allows(db) -> None:
    from llm.cost_tracker import DailyCostTracker

    td = date(2026, 4, 22)
    ts = datetime.combine(td, datetime.min.time().replace(hour=10), tzinfo=timezone.utc).isoformat()
    _seed_call(db, ts, cost=4.99)
    tracker = DailyCostTracker(db, cap_usd=Decimal("5"))
    assert tracker.is_over_cap(td) is False
    assert tracker.remaining_today(td) == Decimal("0.01")


def test_cap_is_per_day(db) -> None:
    from llm.cost_tracker import DailyCostTracker

    td = date(2026, 4, 22)
    ts_yesterday = datetime.combine(date(2026, 4, 21), datetime.min.time().replace(hour=10), tzinfo=timezone.utc).isoformat()
    _seed_call(db, ts_yesterday, cost=10.00)  # yesterday — irrelevant
    tracker = DailyCostTracker(db, cap_usd=Decimal("5"))
    assert tracker.is_over_cap(td) is False


def test_judge_skips_when_cap_hit(db) -> None:
    """Exit criterion: LLM cost cap triggers correctly (seed cost > cap → judgment skipped)."""
    from core.signal import Signal
    from llm.cost_tracker import DailyCostTracker
    from llm.judge import LLMJudge
    from storage.snapshot_repo import SnapshotRow

    td = date(2026, 4, 22)
    ts = datetime.combine(td, datetime.min.time().replace(hour=10), tzinfo=timezone.utc).isoformat()
    _seed_call(db, ts, cost=5.00)
    cap_tracker = DailyCostTracker(db, cap_usd=Decimal("5"))

    # Use a client that MUST NOT be called.
    class _BoomClient:
        class messages:
            @staticmethod
            def create(**_):
                raise AssertionError("client should not be called when cap is hit")

    judge = LLMJudge(db, client=_BoomClient(), cost_tracker=cap_tracker)
    sig = Signal(symbol="AAPL", side="buy", strategy="mean_reversion",
                 confidence=1.0, reason="t", tick_id=1)
    snap = SnapshotRow(symbol="AAPL", ts=datetime.now(timezone.utc), price=180.0,
                      rsi14=55.0, sma20=175.0, sma50=170.0, sma200=160.0,
                      avg_vol_20=3e6, atr14=1.5, price_vs_sma50_pct=5.88)
    result = judge.judge(signal=sig, snapshot=snap, trading_date=td)
    assert result.skipped is True
    assert result.branch == "rule_only"
    assert result.reason == "cost_cap_hit"
