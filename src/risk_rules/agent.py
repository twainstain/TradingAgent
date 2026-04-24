"""Risk Agent — composes rules into a RuleBasedPolicy, persists decisions,
emits FLATTEN_ALL when the kill switch is engaged.

CLAUDE.md invariants enforced here:
  #1 The LLM cannot override risk — this module has zero LLM calls.
  #2 Strategy never sends orders — we return Approved/Rejected, not orders.
  #4 Kill switch is a local file — `KillSwitchRule` checks the path; agent
     emits a single FLATTEN_ALL signal name when engaged.
  #7 Daily loss halt is sticky — `DailyHaltRule` reads `risk_state` rows
     that survive process restart.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from trading_platform.contracts import RiskVerdict
from trading_platform.persistence.db import DbConnection
from trading_platform.risk import RuleBasedPolicy

from core.signal import Signal
from risk_rules.kill_switch import FLATTEN_ALL_SIGNAL, is_engaged as kill_switch_engaged
from risk_rules.portfolio import Portfolio
from risk_rules.rules import default_rules
from risk_rules.sizing import size_order
from storage.risk_decision_repo import write_decision

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    approved: bool
    reason: str
    sized_qty: int
    details: dict[str, Any]
    rule: str  # which rule produced the verdict (or 'all_rules_passed' on approve)


@dataclass(frozen=True)
class AgentResult:
    decision: Decision
    flatten_all: bool = False


class RiskAgent:
    def __init__(
        self,
        db: DbConnection,
        *,
        rules: list | None = None,
        simulation_mode: bool = False,
    ) -> None:
        self._db = db
        self._policy = RuleBasedPolicy(
            rules=rules if rules is not None else default_rules(),
            simulation_mode=simulation_mode,
        )

    @property
    def policy(self) -> RuleBasedPolicy:
        return self._policy

    def _context(
        self,
        signal: Signal,
        *,
        portfolio: Portfolio,
        price: Decimal,
        sized_qty: int,
        now_et: datetime,
        trading_date: date,
        earnings=None,
        kill_switch_path=None,
    ) -> dict[str, Any]:
        return {
            "portfolio": portfolio,
            "sized_qty": sized_qty,
            "price": price,
            "now_et": now_et,
            "trading_date": trading_date,
            "db": self._db,
            "earnings": earnings,
            "kill_switch_path": kill_switch_path,
        }

    def evaluate(
        self,
        signal: Signal,
        *,
        portfolio: Portfolio,
        price: Decimal,
        now_et: datetime,
        trading_date: date,
        earnings=None,
        kill_switch_path=None,
        signal_id: int | None = None,
    ) -> AgentResult:
        """Evaluate a single signal end-to-end. Writes to `risk_decisions`
        if `signal_id` is provided. Returns an AgentResult — the caller
        (execution agent) reads `.decision.approved` and `.flatten_all`.
        """
        qty = size_order(
            equity=portfolio.equity,
            current_exposure=portfolio.total_exposure,
            price=price,
        )
        ctx = self._context(
            signal,
            portfolio=portfolio,
            price=price,
            sized_qty=qty,
            now_et=now_et,
            trading_date=trading_date,
            earnings=earnings,
            kill_switch_path=kill_switch_path,
        )

        verdict: RiskVerdict = self._policy.evaluate(signal, **ctx)

        # Which rule produced the veto? RuleBasedPolicy returns the verdict
        # from the first failing rule; pulling its reason as the rule tag.
        rule_tag = verdict.reason if not verdict.approved else "all_rules_passed"

        # Kill-switch side-effect: if engaged AND we hold any position, emit FLATTEN_ALL.
        flatten_all = False
        if (
            not verdict.approved
            and rule_tag == "kill_switch_engaged"
            and portfolio.open_position_count > 0
        ):
            flatten_all = True
            log.warning(
                "KILL switch engaged with %d open positions — emitting %s",
                portfolio.open_position_count,
                FLATTEN_ALL_SIGNAL,
            )

        decision = Decision(
            approved=bool(verdict.approved),
            reason=verdict.reason,
            sized_qty=qty if verdict.approved else 0,
            details=dict(verdict.details or {}),
            rule=rule_tag,
        )

        if signal_id is not None:
            write_decision(
                self._db,
                signal_id=signal_id,
                approved=decision.approved,
                reason=decision.reason,
                sized_qty=decision.sized_qty if decision.approved else None,
                created_at=datetime.now(timezone.utc),
            )

        return AgentResult(decision=decision, flatten_all=flatten_all)


__all__ = ["RiskAgent", "Decision", "AgentResult", "FLATTEN_ALL_SIGNAL"]
