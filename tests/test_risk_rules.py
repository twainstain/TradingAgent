"""Phase 3: risk rules, sizing, daily halt persistence, and RiskAgent
end-to-end. One test per rule is a Phase 3 exit criterion.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from core.signal import Signal
from risk_rules.earnings import StaticEarningsCalendar
from risk_rules.portfolio import PositionInfo, Portfolio
from risk_rules.rules import (
    DailyHaltRule,
    EarningsBlackoutRule,
    KillSwitchRule,
    MaxOpenPositionsRule,
    MaxPositionSizeRule,
    MaxTotalExposureRule,
    TradingHoursRule,
)
from risk_rules.sizing import size_order


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


def _signal(symbol: str = "AAPL", side: str = "buy") -> Signal:
    return Signal(
        symbol=symbol,
        side=side,
        strategy="mean_reversion",
        confidence=1.0,
        reason="test",
        tick_id=1,
    )


def _portfolio(equity=Decimal("100000"), positions=None, cash=Decimal("50000")) -> Portfolio:
    return Portfolio(equity=equity, cash=cash, positions=positions or {})


def _position(symbol: str, qty: Decimal, market_value: Decimal) -> PositionInfo:
    return PositionInfo(
        symbol=symbol,
        qty=qty,
        market_value=market_value,
        avg_entry_price=market_value / qty if qty else Decimal("0"),
    )


def _during_hours() -> datetime:
    return datetime.combine(date(2026, 4, 22), time(10, 30, tzinfo=timezone.utc))


def _outside_hours() -> datetime:
    return datetime.combine(date(2026, 4, 22), time(16, 30, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

def test_sizing_per_symbol_cap_dominates() -> None:
    # equity 100k, 3% = 3000, no exposure. At $150/share → floor(3000/150) = 20.
    assert size_order(Decimal("100000"), Decimal("0"), Decimal("150")) == 20


def test_sizing_total_exposure_cap_dominates() -> None:
    # equity 100k, 50% cap = 50k. Already at 49k exposure → 1k remaining budget.
    # Per-symbol cap = 3k. min(3k, 1k) / 100 = 10 shares.
    assert size_order(Decimal("100000"), Decimal("49000"), Decimal("100")) == 10


def test_sizing_zero_when_total_cap_breached() -> None:
    assert size_order(Decimal("100000"), Decimal("50000"), Decimal("100")) == 0


def test_sizing_floor_rounding() -> None:
    # Per-symbol budget 3000 / price 130 = 23.07 → floor → 23
    assert size_order(Decimal("100000"), Decimal("0"), Decimal("130")) == 23


def test_sizing_coerces_floats() -> None:
    # Floats accepted at boundary; math stays Decimal internally.
    assert size_order(100000, 0, 150.0) == 20


def test_sizing_rejects_zero_price() -> None:
    assert size_order(Decimal("100000"), Decimal("0"), Decimal("0")) == 0


# ---------------------------------------------------------------------------
# One test per rule (Phase 3 exit criterion)
# ---------------------------------------------------------------------------

def test_kill_switch_rule_rejects_when_file_exists(tmp_path) -> None:
    kill = tmp_path / "KILL"
    kill.write_text("engaged")
    v = KillSwitchRule().evaluate(_signal(), {"kill_switch_path": kill})
    assert v.approved is False
    assert v.reason == "kill_switch_engaged"


def test_kill_switch_rule_approves_when_absent(tmp_path) -> None:
    v = KillSwitchRule().evaluate(_signal(), {"kill_switch_path": tmp_path / "KILL"})
    assert v.approved is True


def test_daily_halt_rule_rejects_when_halted(db) -> None:
    from storage.risk_state_repo import mark_halted

    td = date(2026, 4, 22)
    mark_halted(db, td, reason="daily_loss_exceeded:2.10%")
    v = DailyHaltRule().evaluate(_signal(), {"db": db, "trading_date": td})
    assert v.approved is False
    assert v.reason == "daily_halt_engaged"


def test_daily_halt_rule_approves_when_clear(db) -> None:
    td = date(2026, 4, 22)
    v = DailyHaltRule().evaluate(_signal(), {"db": db, "trading_date": td})
    assert v.approved is True


def test_trading_hours_rule_during() -> None:
    v = TradingHoursRule().evaluate(_signal(), {"now_et": _during_hours()})
    assert v.approved is True


def test_trading_hours_rule_outside() -> None:
    v = TradingHoursRule().evaluate(_signal(), {"now_et": _outside_hours()})
    assert v.approved is False
    assert v.reason == "outside_trading_hours"


def test_max_open_positions_rejects_at_limit() -> None:
    positions = {
        f"SYM{i}": _position(f"SYM{i}", Decimal("10"), Decimal("1000"))
        for i in range(8)
    }
    pf = _portfolio(positions=positions)
    v = MaxOpenPositionsRule().evaluate(_signal("AAPL"), {"portfolio": pf})
    assert v.approved is False
    assert v.reason == "max_open_positions_reached"


def test_max_open_positions_allows_add_to_existing() -> None:
    # Hit the 8-position limit but signal is for a symbol we already hold → allowed.
    positions = {
        f"SYM{i}": _position(f"SYM{i}", Decimal("10"), Decimal("1000"))
        for i in range(8)
    }
    # Rename SYM0 → AAPL so we "already hold" it.
    positions["AAPL"] = positions.pop("SYM0")
    positions["AAPL"] = _position("AAPL", Decimal("10"), Decimal("1000"))
    pf = _portfolio(positions=positions)
    v = MaxOpenPositionsRule().evaluate(_signal("AAPL"), {"portfolio": pf})
    assert v.approved is True


def test_max_position_size_rejects_above_cap() -> None:
    # equity 100k, 3% cap = 3000. Proposing 30 shares @ $150 = 4500 → reject.
    ctx = {
        "portfolio": _portfolio(),
        "sized_qty": 30,
        "price": Decimal("150"),
    }
    v = MaxPositionSizeRule().evaluate(_signal("AAPL"), ctx)
    assert v.approved is False
    assert v.reason == "per_symbol_cap_breached"


def test_max_position_size_approves_under_cap() -> None:
    ctx = {
        "portfolio": _portfolio(),
        "sized_qty": 10,
        "price": Decimal("150"),
    }
    v = MaxPositionSizeRule().evaluate(_signal("AAPL"), ctx)
    assert v.approved is True


def test_max_total_exposure_rejects_above_50pct() -> None:
    # existing 48k + proposed 3k = 51k > 50k cap.
    positions = {"X": _position("X", Decimal("100"), Decimal("48000"))}
    ctx = {
        "portfolio": _portfolio(positions=positions),
        "sized_qty": 30,
        "price": Decimal("100"),  # 30 * 100 = 3000
    }
    v = MaxTotalExposureRule().evaluate(_signal("AAPL"), ctx)
    assert v.approved is False
    assert v.reason == "total_exposure_cap_breached"


def test_max_total_exposure_approves_under_50pct() -> None:
    positions = {"X": _position("X", Decimal("100"), Decimal("20000"))}
    ctx = {
        "portfolio": _portfolio(positions=positions),
        "sized_qty": 20,
        "price": Decimal("100"),  # 20 * 100 = 2000, 20k + 2k = 22k < 50k
    }
    v = MaxTotalExposureRule().evaluate(_signal("AAPL"), ctx)
    assert v.approved is True


def test_earnings_blackout_rule_rejects_within_window() -> None:
    td = date(2026, 4, 22)
    calendar = StaticEarningsCalendar({"AAPL": [date(2026, 4, 24)]})  # 2 days away
    ctx = {"earnings": calendar, "trading_date": td}
    v = EarningsBlackoutRule().evaluate(_signal("AAPL"), ctx)
    assert v.approved is False
    assert v.reason == "earnings_blackout"


def test_earnings_blackout_rule_approves_outside_window() -> None:
    td = date(2026, 4, 22)
    calendar = StaticEarningsCalendar({"AAPL": [date(2026, 5, 1)]})  # > 2 days
    ctx = {"earnings": calendar, "trading_date": td}
    v = EarningsBlackoutRule().evaluate(_signal("AAPL"), ctx)
    assert v.approved is True


# ---------------------------------------------------------------------------
# Daily halt persistence
# ---------------------------------------------------------------------------

def test_daily_halt_trips_at_2pct_loss(db) -> None:
    from risk_rules.daily_halt import DailyHaltBreaker

    td = date(2026, 4, 22)
    breaker = DailyHaltBreaker(db)
    st = breaker.check(
        trading_date=td,
        starting_equity=Decimal("100000"),
        current_equity=Decimal("97900"),  # -2.1% loss
    )
    assert st.halted is True
    assert "daily_loss_exceeded" in st.reason


def test_daily_halt_does_not_trip_at_1pct_loss(db) -> None:
    from risk_rules.daily_halt import DailyHaltBreaker

    td = date(2026, 4, 22)
    breaker = DailyHaltBreaker(db)
    breaker.check(
        trading_date=td,
        starting_equity=Decimal("100000"),
        current_equity=Decimal("99000"),  # -1%
    )
    assert breaker.is_halted(td) is False


def test_daily_halt_survives_reconnect(tmp_path) -> None:
    """Halt row persists across a DB close/reopen — simulates process restart."""
    from trading_platform.persistence.db import close_db, init_db

    from risk_rules.daily_halt import DailyHaltBreaker

    schema = (Path(__file__).resolve().parent.parent / "src" / "schema.sql").read_text()
    db_path = tmp_path / "events.db"
    td = date(2026, 4, 22)

    db1 = init_db(db_path, schema)
    DailyHaltBreaker(db1).check(
        trading_date=td,
        starting_equity=Decimal("100000"),
        current_equity=Decimal("97000"),
    )
    assert DailyHaltBreaker(db1).is_halted(td) is True
    close_db()

    # Reopen: same date still halted.
    db2 = init_db(db_path, schema)
    assert DailyHaltBreaker(db2).is_halted(td) is True
    # Next trading date is clear.
    assert DailyHaltBreaker(db2).is_halted(td + timedelta(days=1)) is False
    close_db()


# ---------------------------------------------------------------------------
# RiskAgent integration
# ---------------------------------------------------------------------------

def _insert_tick_and_signal(db, symbol: str = "AAPL") -> tuple[int, int]:
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
    return tick_id, signal_id


def test_risk_agent_approves_happy_path_and_persists(db) -> None:
    from risk_rules.agent import RiskAgent

    _, signal_id = _insert_tick_and_signal(db)
    sig = _signal("AAPL")
    pf = _portfolio()
    agent = RiskAgent(db)
    result = agent.evaluate(
        sig,
        portfolio=pf,
        price=Decimal("150"),
        now_et=_during_hours(),
        trading_date=date(2026, 4, 22),
        earnings=StaticEarningsCalendar({}),
        signal_id=signal_id,
    )
    assert result.decision.approved is True
    assert result.decision.sized_qty == 20  # 3% of 100k / 150
    assert result.flatten_all is False

    row = db.execute("SELECT approved, sized_qty FROM risk_decisions WHERE signal_id = ?", (signal_id,)).fetchone()
    assert row["approved"] == 1
    assert row["sized_qty"] == 20


def test_risk_agent_kill_switch_with_positions_emits_flatten_all(db, tmp_path) -> None:
    from risk_rules.agent import RiskAgent

    kill = tmp_path / "KILL"
    kill.write_text("engaged")
    _, signal_id = _insert_tick_and_signal(db)
    pf = _portfolio(positions={"MSFT": _position("MSFT", Decimal("10"), Decimal("4000"))})
    result = RiskAgent(db).evaluate(
        _signal("AAPL"),
        portfolio=pf,
        price=Decimal("150"),
        now_et=_during_hours(),
        trading_date=date(2026, 4, 22),
        earnings=StaticEarningsCalendar({}),
        kill_switch_path=kill,
        signal_id=signal_id,
    )
    assert result.decision.approved is False
    assert result.decision.reason == "kill_switch_engaged"
    assert result.flatten_all is True


def test_risk_agent_kill_switch_no_positions_no_flatten(db, tmp_path) -> None:
    from risk_rules.agent import RiskAgent

    kill = tmp_path / "KILL"
    kill.write_text("")
    _, signal_id = _insert_tick_and_signal(db)
    result = RiskAgent(db).evaluate(
        _signal("AAPL"),
        portfolio=_portfolio(),  # zero positions
        price=Decimal("150"),
        now_et=_during_hours(),
        trading_date=date(2026, 4, 22),
        earnings=StaticEarningsCalendar({}),
        kill_switch_path=kill,
        signal_id=signal_id,
    )
    assert result.decision.approved is False
    assert result.decision.reason == "kill_switch_engaged"
    assert result.flatten_all is False  # nothing to flatten


def test_risk_agent_persists_rejections_too(db, tmp_path) -> None:
    from risk_rules.agent import RiskAgent

    kill = tmp_path / "KILL"
    kill.write_text("")
    _, signal_id = _insert_tick_and_signal(db)
    RiskAgent(db).evaluate(
        _signal("AAPL"),
        portfolio=_portfolio(),
        price=Decimal("150"),
        now_et=_during_hours(),
        trading_date=date(2026, 4, 22),
        earnings=StaticEarningsCalendar({}),
        kill_switch_path=kill,
        signal_id=signal_id,
    )
    row = db.execute("SELECT approved, reason, sized_qty FROM risk_decisions WHERE signal_id = ?", (signal_id,)).fetchone()
    assert row["approved"] == 0
    assert row["reason"] == "kill_switch_engaged"
    assert row["sized_qty"] is None
