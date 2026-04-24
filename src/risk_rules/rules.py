"""Risk rules (ARCHITECTURE §3.3).

Each rule is a tiny dataclass with a `name` and an `evaluate(candidate,
context)` method returning a `RiskVerdict`. No LLM, no network (earnings
is handled by an injected calendar so the rule itself stays pure).

`candidate` is a `Signal`. `context` is a dict with at minimum:
  - portfolio: Portfolio
  - sized_qty: int
  - price: Decimal
  - now_et: datetime  (ET-localized)
  - trading_date: date (ET)

Optional context keys (when available):
  - earnings: EarningsCalendar  (skip earnings rule if absent)
  - kill_switch_path: Path
  - db: DbConnection             (for daily halt)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_platform.contracts import RiskVerdict

from risk_rules.kill_switch import is_engaged as kill_switch_engaged
from risk_rules.sizing import MAX_PER_SYMBOL_PCT, MAX_TOTAL_EXPOSURE_PCT
from storage.risk_state_repo import is_halted_today

# Trading hours per ARCHITECTURE §3.3 — ET, inclusive both ends.
OPEN_ET = time(9, 45)
CLOSE_ET = time(15, 45)
MAX_OPEN_POSITIONS = 8


def _decimal(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


# ---------------------------------------------------------------------------
# 1. Kill switch — hard rejection with side-effect (FLATTEN_ALL handled by the
#    risk agent, not the rule).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class KillSwitchRule:
    path: Path | None = None
    name: str = "kill_switch"

    def evaluate(self, candidate: Any, context: dict[str, Any]) -> RiskVerdict:
        path = self.path or context.get("kill_switch_path")
        if kill_switch_engaged(path):
            return RiskVerdict(False, "kill_switch_engaged", {"path": str(path) if path else "default"})
        return RiskVerdict(True, "kill_switch_not_engaged")


# ---------------------------------------------------------------------------
# 2. Daily loss halt — sticky, reads from `risk_state` table.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DailyHaltRule:
    name: str = "daily_halt"

    def evaluate(self, candidate: Any, context: dict[str, Any]) -> RiskVerdict:
        db = context.get("db")
        trading_date = context.get("trading_date")
        if db is None or trading_date is None:
            # No state to check → fail OPEN, but flag it.
            return RiskVerdict(True, "daily_halt_unknown", {"warning": "no db/trading_date in context"})
        if is_halted_today(db, trading_date):
            return RiskVerdict(False, "daily_halt_engaged", {"trading_date": str(trading_date)})
        return RiskVerdict(True, "daily_halt_clear")


# ---------------------------------------------------------------------------
# 3. Trading hours (09:45 – 15:45 ET).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TradingHoursRule:
    open_et: time = OPEN_ET
    close_et: time = CLOSE_ET
    name: str = "trading_hours"

    def evaluate(self, candidate: Any, context: dict[str, Any]) -> RiskVerdict:
        now_et: datetime | None = context.get("now_et")
        if now_et is None:
            return RiskVerdict(False, "trading_hours_no_time", {})
        t = now_et.timetz().replace(tzinfo=None)
        if not (self.open_et <= t <= self.close_et):
            return RiskVerdict(
                False,
                "outside_trading_hours",
                {"now_et": now_et.isoformat(), "window": f"{self.open_et}-{self.close_et}"},
            )
        return RiskVerdict(True, "within_trading_hours")


# ---------------------------------------------------------------------------
# 4. Max open positions (8).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MaxOpenPositionsRule:
    limit: int = MAX_OPEN_POSITIONS
    name: str = "max_open_positions"

    def evaluate(self, candidate: Any, context: dict[str, Any]) -> RiskVerdict:
        portfolio = context.get("portfolio")
        if portfolio is None:
            return RiskVerdict(False, "no_portfolio", {})
        # If we already hold this symbol, we're not opening a new position.
        sym = candidate.symbol.upper()
        holding = portfolio.positions.get(sym)
        if holding is not None and holding.qty != 0:
            return RiskVerdict(True, "existing_position_allows_add")
        if portfolio.open_position_count >= self.limit:
            return RiskVerdict(
                False,
                "max_open_positions_reached",
                {"open": portfolio.open_position_count, "limit": self.limit},
            )
        return RiskVerdict(True, "within_open_position_limit")


# ---------------------------------------------------------------------------
# 5. Per-symbol 3% cap — compares the proposed trade + existing exposure
#    against the per-symbol budget.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MaxPositionSizeRule:
    max_pct: Decimal = MAX_PER_SYMBOL_PCT
    name: str = "max_position_size"

    def evaluate(self, candidate: Any, context: dict[str, Any]) -> RiskVerdict:
        portfolio = context.get("portfolio")
        qty = context.get("sized_qty")
        price = context.get("price")
        if portfolio is None or qty is None or price is None:
            return RiskVerdict(False, "max_position_size_missing_context", {})
        eq = portfolio.equity
        if eq <= 0:
            return RiskVerdict(False, "zero_equity", {})
        proposed_value = _decimal(qty) * _decimal(price)
        existing = portfolio.exposure_for(candidate.symbol)
        new_symbol_exposure = existing + proposed_value
        cap = eq * self.max_pct
        if new_symbol_exposure > cap:
            return RiskVerdict(
                False,
                "per_symbol_cap_breached",
                {
                    "symbol": candidate.symbol,
                    "existing": str(existing),
                    "proposed": str(proposed_value),
                    "cap": str(cap),
                },
            )
        return RiskVerdict(True, "within_per_symbol_cap")


# ---------------------------------------------------------------------------
# 6. Max total exposure 50%.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MaxTotalExposureRule:
    max_pct: Decimal = MAX_TOTAL_EXPOSURE_PCT
    name: str = "max_total_exposure"

    def evaluate(self, candidate: Any, context: dict[str, Any]) -> RiskVerdict:
        portfolio = context.get("portfolio")
        qty = context.get("sized_qty")
        price = context.get("price")
        if portfolio is None or qty is None or price is None:
            return RiskVerdict(False, "max_total_exposure_missing_context", {})
        proposed_value = _decimal(qty) * _decimal(price)
        cap = portfolio.equity * self.max_pct
        new_total = portfolio.total_exposure + proposed_value
        if new_total > cap:
            return RiskVerdict(
                False,
                "total_exposure_cap_breached",
                {"new_total": str(new_total), "cap": str(cap)},
            )
        return RiskVerdict(True, "within_total_exposure_cap")


# ---------------------------------------------------------------------------
# 7. Earnings blackout — 2-day window before next earnings.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EarningsBlackoutRule:
    window_days: int = 2
    name: str = "earnings_blackout"

    def evaluate(self, candidate: Any, context: dict[str, Any]) -> RiskVerdict:
        calendar = context.get("earnings")
        trading_date = context.get("trading_date")
        if calendar is None:
            return RiskVerdict(True, "earnings_skip_no_calendar", {"warning": "no earnings calendar"})
        in_blackout, earnings_date = calendar.in_blackout(
            candidate.symbol, today=trading_date, window_days=self.window_days
        )
        if in_blackout:
            return RiskVerdict(
                False,
                "earnings_blackout",
                {"symbol": candidate.symbol, "earnings_date": str(earnings_date)},
            )
        return RiskVerdict(True, "earnings_clear")


def default_rules() -> list:
    """Build the full ordered rule list per ARCHITECTURE §3.3.

    Order matters: cheap + hard rules first (kill switch, halt, hours);
    portfolio/sizing rules after; earnings last (may hit Polygon).
    """
    return [
        KillSwitchRule(),
        DailyHaltRule(),
        TradingHoursRule(),
        MaxOpenPositionsRule(),
        MaxPositionSizeRule(),
        MaxTotalExposureRule(),
        EarningsBlackoutRule(),
    ]
