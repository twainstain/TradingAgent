"""NYSE session gate via `pandas_market_calendars`.

Tick cadence (ARCHITECTURE §3.5 / EXECUTION_PLAN §Phase 5):
  - 5 minutes within 09:45 ET → close−15min ET
  - idle otherwise (holidays, half-days honored)

Half-days: close-of-session is determined by the calendar's `market_close`
for the date, so early-close days auto-shift the window end to close-15min.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal

ET = ZoneInfo("America/New_York")

# Trading window — relative to the session's open/close.
WINDOW_OPEN_SHIFT = timedelta(minutes=15)   # 09:30 + 15 = 09:45 ET on a normal day
WINDOW_CLOSE_SHIFT = timedelta(minutes=-15)  # close − 15 min (half-day: close is earlier)


@dataclass(frozen=True)
class Session:
    trading_date: date
    open_utc: datetime
    close_utc: datetime

    @property
    def window_start_utc(self) -> datetime:
        return self.open_utc + WINDOW_OPEN_SHIFT

    @property
    def window_end_utc(self) -> datetime:
        return self.close_utc + WINDOW_CLOSE_SHIFT


class NYSECalendar:
    def __init__(self) -> None:
        self._cal = mcal.get_calendar("NYSE")

    def session_on(self, trading_date: date) -> Session | None:
        sched = self._cal.schedule(start_date=trading_date, end_date=trading_date)
        if sched.empty:
            return None
        row = sched.iloc[0]
        return Session(
            trading_date=trading_date,
            open_utc=row["market_open"].to_pydatetime(),
            close_utc=row["market_close"].to_pydatetime(),
        )

    def is_trading_day(self, d: date) -> bool:
        return self.session_on(d) is not None

    def in_window(self, now: datetime) -> tuple[bool, Session | None]:
        """True iff `now` is inside the 09:45-ET → close−15min ET window
        for today's NYSE session. Returns the Session for context/logging.
        """
        now_utc = now.astimezone(ZoneInfo("UTC"))
        et_date = now.astimezone(ET).date()
        session = self.session_on(et_date)
        if session is None:
            return False, None
        in_win = session.window_start_utc <= now_utc <= session.window_end_utc
        return in_win, session


def et_trading_date(now: datetime) -> date:
    return now.astimezone(ET).date()


def next_tick(
    now: datetime,
    *,
    cadence_seconds: int = 300,
    calendar: NYSECalendar | None = None,
) -> datetime:
    """Return the next scheduled tick instant (UTC).

    If currently in-window: now + cadence, clamped to window end.
    If before today's window: today's window_start.
    Else: next trading day's window_start.
    """
    cal = calendar or NYSECalendar()
    in_win, session = cal.in_window(now)
    now_utc = now.astimezone(ZoneInfo("UTC"))
    if in_win and session is not None:
        return min(now_utc + timedelta(seconds=cadence_seconds), session.window_end_utc)

    # Find the next session that has a window_start > now.
    et_today = now.astimezone(ET).date()
    for offset in range(0, 8):  # look ahead up to 8 calendar days (covers long weekends)
        d = et_today + timedelta(days=offset)
        s = cal.session_on(d)
        if s is None:
            continue
        if s.window_start_utc > now_utc:
            return s.window_start_utc
    # Fallback: +1d, window open approximation.
    return datetime.combine(et_today + timedelta(days=1), time(13, 45), ZoneInfo("UTC"))
