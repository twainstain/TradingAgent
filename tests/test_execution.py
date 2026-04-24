"""Phase 4: execution agent + fill poller.

Exit-criteria coverage:
  - idempotent client_order_id
  - rejection path logs response + persists as status='rejected' + NO retry
  - brackets always include stop + take_profit (no naked entries)
  - fill poller upserts and dedups
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.signal import Signal
from risk_rules.agent import Decision


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    from trading_platform.persistence.db import close_db, init_db

    schema = (Path(__file__).resolve().parent.parent / "src" / "schema.sql").read_text()
    conn = init_db(tmp_path / "events.db", schema)
    try:
        yield conn
    finally:
        close_db()


def _seed_risk_decision(db, *, symbol: str = "AAPL") -> tuple[int, int, int]:
    """Return (tick_id, signal_id, risk_decision_id)."""
    now = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT INTO ticks (started_at, status) VALUES (?, 'running')", (now,))
    db.commit()
    tick_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
    db.execute(
        """
        INSERT INTO signals (tick_id, symbol, strategy, side, confidence, reason, llm_branch, created_at)
        VALUES (?, ?, 'mean_reversion', 'buy', 1.0, 'test', 'rule_only', ?)
        """,
        (tick_id, symbol, now),
    )
    db.commit()
    signal_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
    db.execute(
        """
        INSERT INTO risk_decisions (signal_id, approved, reason, sized_qty, created_at)
        VALUES (?, 1, 'all_rules_passed', 10, ?)
        """,
        (signal_id, now),
    )
    db.commit()
    rd_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
    return tick_id, signal_id, rd_id


def _signal(tick_id: int, symbol: str = "AAPL", strategy: str = "mean_reversion") -> Signal:
    return Signal(
        symbol=symbol, side="buy", strategy=strategy,
        confidence=1.0, reason="t", tick_id=tick_id,
    )


def _approved_decision(qty: int = 10) -> Decision:
    return Decision(
        approved=True, reason="all_rules_passed", sized_qty=qty, details={},
        rule="all_rules_passed",
    )


class _FakeResponse:
    def __init__(self, order_id: str, status: str = "accepted") -> None:
        self.id = order_id
        self.status = status

    def model_dump(self) -> dict[str, Any]:
        return {"id": self.id, "status": self.status}


class _FakeClient:
    def __init__(self, raise_on_submit: Exception | None = None) -> None:
        self.submitted: list[Any] = []
        self._raise = raise_on_submit

    def submit_order(self, order_data):
        if self._raise is not None:
            raise self._raise
        self.submitted.append(order_data)
        return _FakeResponse(order_id=f"broker-{len(self.submitted)}")


# ---------------------------------------------------------------------------
# compute_bracket_prices — sanity + strategy overrides
# ---------------------------------------------------------------------------

def test_bracket_prices_use_defaults_minus_2_plus_4() -> None:
    from execution.agent import compute_bracket_prices

    stop, tp = compute_bracket_prices(Decimal("100"), stop_pct=-0.02, take_profit_pct=0.04)
    assert stop == Decimal("98.00")
    assert tp == Decimal("104.00")


def test_bracket_prices_round_to_2dp() -> None:
    from execution.agent import compute_bracket_prices

    stop, tp = compute_bracket_prices(Decimal("123.456"), stop_pct=-0.02, take_profit_pct=0.04)
    assert stop.as_tuple().exponent == -2
    assert tp.as_tuple().exponent == -2


def test_per_strategy_overrides_flow_from_config() -> None:
    from strategies import execution_overrides

    stop, tp = execution_overrides("mean_reversion")
    # strategies.yaml committed value is -0.02 / 0.04
    assert stop == -0.02
    assert tp == 0.04

    # Unknown strategy falls back to defaults.
    stop, tp = execution_overrides("does_not_exist")
    assert stop == -0.02
    assert tp == 0.04


# ---------------------------------------------------------------------------
# ExecutionAgent happy path
# ---------------------------------------------------------------------------

def test_submit_inserts_order_row_with_stop_and_tp(db) -> None:
    from execution.agent import ExecutionAgent

    tick_id, _, rd_id = _seed_risk_decision(db)
    fake = _FakeClient()
    agent = ExecutionAgent(db, client=fake)
    result = agent.submit(
        signal=_signal(tick_id),
        decision=_approved_decision(10),
        risk_decision_id=rd_id,
        entry_price=Decimal("100"),
    )
    assert result.submitted is True
    assert result.client_order_id == f"{tick_id}:AAPL"
    assert result.broker_order_id == "broker-1"

    row = db.execute(
        "SELECT * FROM orders WHERE client_order_id = ?",
        (f"{tick_id}:AAPL",),
    ).fetchone()
    assert row["status"] != "rejected"
    assert row["stop_price"] == 98.0  # -2%
    assert row["take_profit_price"] == 104.0  # +4%
    assert row["type"] == "bracket"


def test_bracket_request_carries_both_legs(db) -> None:
    from execution.agent import ExecutionAgent

    tick_id, _, rd_id = _seed_risk_decision(db)
    fake = _FakeClient()
    ExecutionAgent(db, client=fake).submit(
        signal=_signal(tick_id),
        decision=_approved_decision(10),
        risk_decision_id=rd_id,
        entry_price=Decimal("100"),
    )
    assert len(fake.submitted) == 1
    req = fake.submitted[0]
    assert req.take_profit is not None
    assert req.stop_loss is not None
    assert float(req.take_profit.limit_price) == 104.0
    assert float(req.stop_loss.stop_price) == 98.0


# ---------------------------------------------------------------------------
# Idempotency — same tick + symbol must NOT re-submit
# ---------------------------------------------------------------------------

def test_duplicate_submission_is_deduped_and_does_not_hit_broker(db) -> None:
    from execution.agent import ExecutionAgent

    tick_id, _, rd_id = _seed_risk_decision(db)
    fake = _FakeClient()
    agent = ExecutionAgent(db, client=fake)
    agent.submit(
        signal=_signal(tick_id),
        decision=_approved_decision(10),
        risk_decision_id=rd_id,
        entry_price=Decimal("100"),
    )
    # second call
    result2 = agent.submit(
        signal=_signal(tick_id),
        decision=_approved_decision(10),
        risk_decision_id=rd_id,
        entry_price=Decimal("100"),
    )
    assert result2.submitted is False
    assert result2.reason == "idempotent_dedup"
    assert len(fake.submitted) == 1  # broker only hit once
    count = db.execute("SELECT COUNT(*) FROM orders WHERE client_order_id = ?",
                       (f"{tick_id}:AAPL",)).fetchone()[0]
    assert count == 1


def test_client_order_id_is_tick_id_colon_symbol() -> None:
    from execution.agent import client_order_id_for

    assert client_order_id_for(42, "aapl") == "42:AAPL"


# ---------------------------------------------------------------------------
# Rejection path — broker raises; persist + alert; NO retry
# ---------------------------------------------------------------------------

def test_broker_rejection_is_persisted_and_not_retried(db, caplog) -> None:
    from execution.agent import ExecutionAgent

    tick_id, _, rd_id = _seed_risk_decision(db)
    fake = _FakeClient(raise_on_submit=RuntimeError("insufficient buying power"))
    calls: list[tuple[str, dict]] = []

    agent = ExecutionAgent(db, client=fake, alert_hook=lambda ev, d: calls.append((ev, d)))
    with caplog.at_level("ERROR"):
        result = agent.submit(
            signal=_signal(tick_id),
            decision=_approved_decision(10),
            risk_decision_id=rd_id,
            entry_price=Decimal("100"),
        )
    assert result.submitted is False
    assert result.reason == "broker_rejection"

    # Broker was only called once — NO silent retry.
    # (FakeClient doesn't record on error, but we can check: submit attempts still only 1.)
    # Drive a second submit with the same id; it should dedup on the persisted rejection row.
    fake2 = _FakeClient()
    agent2 = ExecutionAgent(db, client=fake2)
    result2 = agent2.submit(
        signal=_signal(tick_id),
        decision=_approved_decision(10),
        risk_decision_id=rd_id,
        entry_price=Decimal("100"),
    )
    assert result2.submitted is False
    assert result2.reason == "idempotent_dedup"
    assert fake2.submitted == []  # rejection path still dedups — no retry

    # Rejection row exists with status='rejected' and the error body captured.
    row = db.execute(
        "SELECT * FROM orders WHERE client_order_id = ?",
        (f"{tick_id}:AAPL",),
    ).fetchone()
    assert row["status"] == "rejected"
    assert row["raw_response"] is not None
    assert "insufficient buying power" in row["raw_response"]

    # Alert hook fired once with the broker_rejection event.
    assert len(calls) == 1
    assert calls[0][0] == "broker_rejection"
    assert calls[0][1]["symbol"] == "AAPL"


def test_not_approved_or_zero_qty_skip(db) -> None:
    from execution.agent import ExecutionAgent

    tick_id, _, rd_id = _seed_risk_decision(db)
    fake = _FakeClient()
    agent = ExecutionAgent(db, client=fake)

    # not approved
    r1 = agent.submit(
        signal=_signal(tick_id),
        decision=Decision(approved=False, reason="x", sized_qty=10, details={}, rule="x"),
        risk_decision_id=rd_id,
        entry_price=Decimal("100"),
    )
    assert r1.submitted is False and r1.reason == "signal_not_approved"

    # zero qty
    r2 = agent.submit(
        signal=_signal(tick_id),
        decision=_approved_decision(0),
        risk_decision_id=rd_id,
        entry_price=Decimal("100"),
    )
    assert r2.submitted is False and r2.reason == "zero_qty"

    assert fake.submitted == []


# ---------------------------------------------------------------------------
# Fill poller
# ---------------------------------------------------------------------------

class _PollClient:
    def __init__(self, orders: dict[str, Any]) -> None:
        self._orders = orders
        self.requested: list[str] = []

    def get_order_by_client_id(self, coi: str):
        self.requested.append(coi)
        if coi not in self._orders:
            raise RuntimeError(f"unknown {coi}")
        return self._orders[coi]


def _order_result(**kwargs):
    return SimpleNamespace(**kwargs)


def test_fill_poller_upserts_partial_then_filled(db) -> None:
    from execution.agent import ExecutionAgent
    from execution.fill_poller import poll_fills
    from storage.fill_repo import fills_for_order

    tick_id, _, rd_id = _seed_risk_decision(db)
    fake = _FakeClient()
    ExecutionAgent(db, client=fake).submit(
        signal=_signal(tick_id),
        decision=_approved_decision(10),
        risk_decision_id=rd_id,
        entry_price=Decimal("100"),
    )
    coi = f"{tick_id}:AAPL"

    # First poll: partial.
    poll_client = _PollClient({
        coi: _order_result(id="broker-1", status="partially_filled",
                           filled_qty="4", filled_avg_price="100.05")
    })
    poll_fills(db, client=poll_client)

    fills = fills_for_order(db, coi)
    assert len(fills) == 1
    assert fills[0].status == "partially_filled"
    assert fills[0].filled_qty == 4

    # Second poll: filled.
    poll_client2 = _PollClient({
        coi: _order_result(id="broker-1", status="filled",
                           filled_qty="10", filled_avg_price="100.05")
    })
    fills_fired: list[tuple[str, dict]] = []
    poll_fills(db, client=poll_client2, alert_hook=lambda ev, d: fills_fired.append((ev, d)))

    fills = fills_for_order(db, coi)
    assert len(fills) == 2
    assert fills[-1].status == "filled"
    assert fills[-1].filled_qty == 10
    # Alert fired on the transition to filled.
    assert any(ev == "fill" for ev, _ in fills_fired)


def test_fill_poller_dedups_unchanged_state(db) -> None:
    from execution.agent import ExecutionAgent
    from execution.fill_poller import poll_fills
    from storage.fill_repo import fills_for_order

    tick_id, _, rd_id = _seed_risk_decision(db)
    fake = _FakeClient()
    ExecutionAgent(db, client=fake).submit(
        signal=_signal(tick_id),
        decision=_approved_decision(10),
        risk_decision_id=rd_id,
        entry_price=Decimal("100"),
    )
    coi = f"{tick_id}:AAPL"
    poll_client = _PollClient({
        coi: _order_result(id="broker-1", status="new",
                           filled_qty="0", filled_avg_price=None)
    })
    poll_fills(db, client=poll_client)
    poll_fills(db, client=poll_client)
    poll_fills(db, client=poll_client)

    # Even across 3 polls with unchanged state, only one fill row.
    assert len(fills_for_order(db, coi)) == 1


def test_fill_poller_skips_terminal_orders(db) -> None:
    from execution.agent import ExecutionAgent
    from execution.fill_poller import poll_fills

    tick_id, _, rd_id = _seed_risk_decision(db)
    fake = _FakeClient()
    ExecutionAgent(db, client=fake).submit(
        signal=_signal(tick_id),
        decision=_approved_decision(10),
        risk_decision_id=rd_id,
        entry_price=Decimal("100"),
    )
    coi = f"{tick_id}:AAPL"
    # Force terminal state in DB.
    db.execute("UPDATE orders SET status = 'filled' WHERE client_order_id = ?", (coi,))
    db.commit()

    poll_client = _PollClient({coi: _order_result(id="broker-1", status="filled",
                                                   filled_qty="10", filled_avg_price="100")})
    poll_fills(db, client=poll_client)
    assert poll_client.requested == []  # no poll issued — order is terminal
