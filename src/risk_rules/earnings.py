"""Earnings blackout — 2-day window before the next earnings release.

Uses the Polygon reference API (/vX/reference/tickers/{ticker}/events) to
fetch upcoming earnings. Results are cached in-process with a 6-hour TTL
(earnings calendars don't move in a day).

Fails OPEN (allow) on Polygon outage — but only after logging a warning.
This is consistent with the platform degraded-mode philosophy (if the
data source is down, strategies keep flowing but we flag it). The daily
halt and kill switch remain hard stops regardless.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from trading_platform.data import TTLCache

log = logging.getLogger(__name__)

DEFAULT_BLACKOUT_DAYS = 2
POLYGON_BASE = "https://api.polygon.io"
CACHE_TTL_SECONDS = 6 * 3600  # 6h


class EarningsCalendar:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        cache_ttl_seconds: float = CACHE_TTL_SECONDS,
    ) -> None:
        self._api_key = api_key or os.environ.get("POLYGON_API_KEY", "")
        self._cache: TTLCache = TTLCache(ttl_seconds=cache_ttl_seconds)

    def _fetch(self, symbol: str) -> list[date]:
        """Return a sorted list of upcoming earnings dates for `symbol`."""
        import httpx

        url = f"{POLYGON_BASE}/vX/reference/tickers/{symbol.upper()}/events"
        params = {"types": "ticker_change,earnings", "apiKey": self._api_key}
        try:
            r = httpx.get(url, params=params, timeout=5.0)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("polygon earnings lookup failed for %s: %s", symbol, exc)
            return []
        data = r.json() or {}
        events = (data.get("results") or {}).get("events") or []
        out: list[date] = []
        today = date.today()
        for ev in events:
            if ev.get("type") != "earnings":
                continue
            d_str = ev.get("date")
            if not d_str:
                continue
            try:
                d = date.fromisoformat(d_str[:10])
            except ValueError:
                continue
            if d >= today:
                out.append(d)
        return sorted(out)

    def upcoming(self, symbol: str) -> list[date]:
        key = symbol.upper()
        cached = self._cache.get(key)
        if cached is not None:
            return list(cached)  # type: ignore[arg-type]
        dates = self._fetch(key)
        self._cache.set(key, dates, reason="polygon_lookup")
        return dates

    def in_blackout(
        self,
        symbol: str,
        *,
        today: date | None = None,
        window_days: int = DEFAULT_BLACKOUT_DAYS,
    ) -> tuple[bool, date | None]:
        today = today or date.today()
        upcoming = self.upcoming(symbol)
        for d in upcoming:
            if 0 <= (d - today).days <= window_days:
                return True, d
        return False, None


class StaticEarningsCalendar:
    """In-memory earnings calendar for tests and offline use."""

    def __init__(self, dates_by_symbol: dict[str, Iterable[date]]) -> None:
        self._map: dict[str, list[date]] = {
            k.upper(): sorted(v) for k, v in dates_by_symbol.items()
        }

    def in_blackout(
        self,
        symbol: str,
        *,
        today: date | None = None,
        window_days: int = DEFAULT_BLACKOUT_DAYS,
    ) -> tuple[bool, date | None]:
        today = today or date.today()
        for d in self._map.get(symbol.upper(), []):
            if 0 <= (d - today).days <= window_days:
                return True, d
        return False, None
