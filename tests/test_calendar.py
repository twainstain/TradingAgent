"""Phase 5: NYSE calendar gate — trading day / holiday / half-day."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from orchestrator.calendar import ET, NYSECalendar, next_tick

UTC = ZoneInfo("UTC")


def test_mid_day_weekday_in_window() -> None:
    cal = NYSECalendar()
    # 2026-04-22 is a Wednesday (non-holiday).
    now = datetime(2026, 4, 22, 14, 0, tzinfo=UTC)  # 10:00 ET
    in_win, session = cal.in_window(now)
    assert in_win is True
    assert session is not None
    assert session.trading_date.isoformat() == "2026-04-22"


def test_saturday_out_of_window() -> None:
    cal = NYSECalendar()
    now = datetime(2026, 4, 18, 14, 0, tzinfo=UTC)
    in_win, _ = cal.in_window(now)
    assert in_win is False


def test_mlk_day_holiday_out_of_window() -> None:
    cal = NYSECalendar()
    # MLK Day 2026 = 2026-01-19
    now = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    in_win, _ = cal.in_window(now)
    assert in_win is False


def test_early_before_window_not_yet_in() -> None:
    cal = NYSECalendar()
    # 09:00 ET — before the 09:45 window.
    now = datetime(2026, 4, 22, 13, 0, tzinfo=UTC)
    in_win, _ = cal.in_window(now)
    assert in_win is False


def test_late_after_window_not_in() -> None:
    cal = NYSECalendar()
    # 15:50 ET — close is 16:00, window ends at 15:45.
    now = datetime(2026, 4, 22, 19, 50, tzinfo=UTC)
    in_win, _ = cal.in_window(now)
    assert in_win is False


def test_next_tick_during_window_advances_by_cadence() -> None:
    cal = NYSECalendar()
    now = datetime(2026, 4, 22, 14, 0, tzinfo=UTC)  # 10:00 ET
    nt = next_tick(now, cadence_seconds=300, calendar=cal)
    delta = (nt - now).total_seconds()
    assert 0 < delta <= 300


def test_next_tick_on_holiday_jumps_to_next_session() -> None:
    cal = NYSECalendar()
    # MLK Day 2026-01-19 is a Mon; next session opens Tue 2026-01-20.
    now = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    nt = next_tick(now, calendar=cal)
    # Expect next trading day's window start (09:45 ET → 14:45 UTC).
    assert nt.date().isoformat() == "2026-01-20"
    assert nt.astimezone(ET).time() >= time(9, 45)
