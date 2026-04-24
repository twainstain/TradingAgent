"""Execution Agent — converts Approved signals into Alpaca bracket orders.

CLAUDE.md invariants:
  #2 Strategy never sends orders — only this module calls the broker.
  #3 Paper endpoint by default — the TradingClient honors the `.env` base url.
  #5 Every order is traceable — `orders.risk_decision_id → signals.tick_id` chain.
  #8 Bracket orders, always — no naked entries. Every submission attaches
     entry + stop-loss + take-profit atomically.
  #9 Idempotent orders — `client_order_id = "{tick_id}:{symbol}"` so a replay
     of the same tick can't produce a duplicate fill (broker-side dedup is the
     safety net).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from trading_platform.persistence.db import DbConnection

from core.signal import Signal
from risk_rules.agent import Decision
from storage.order_repo import get_order_by_client_id, insert_order, update_status
from strategies import execution_overrides

log = logging.getLogger(__name__)


def client_order_id_for(tick_id: int, symbol: str) -> str:
    return f"{int(tick_id)}:{symbol.upper()}"


def _round_price(price: Decimal, places: int = 2) -> Decimal:
    q = Decimal("1").scaleb(-places)  # 0.01 for places=2
    return price.quantize(q)


def compute_bracket_prices(
    entry_price: Decimal,
    *,
    stop_pct: float,
    take_profit_pct: float,
) -> tuple[Decimal, Decimal]:
    """Return (stop_price, take_profit_price) rounded to 2 decimals.

    `stop_pct` is a signed fraction (e.g. -0.02), `take_profit_pct` positive.
    """
    stop_p = _round_price(entry_price * (Decimal("1") + Decimal(str(stop_pct))))
    tp_p = _round_price(entry_price * (Decimal("1") + Decimal(str(take_profit_pct))))
    return stop_p, tp_p


@dataclass(frozen=True)
class ExecutionResult:
    submitted: bool
    client_order_id: str
    broker_order_id: str | None = None
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class ExecutionAgent:
    """Submits Alpaca bracket orders for approved signals. Never retries."""

    def __init__(
        self,
        db: DbConnection,
        *,
        client=None,
        alert_hook=None,
    ) -> None:
        self._db = db
        self._client = client  # lazy: _get_client() builds from .env if None
        self._alert_hook = alert_hook  # callable(event: str, details: dict) — Phase 5 wires AlertDispatcher

    def _get_client(self):
        if self._client is not None:
            return self._client
        from alpaca.trading.client import TradingClient

        key = os.environ["ALPACA_API_KEY"]
        secret = os.environ["ALPACA_API_SECRET"]
        base = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
        self._client = TradingClient(key, secret, paper="paper" in base)
        return self._client

    def _alert(self, event: str, details: dict[str, Any]) -> None:
        if self._alert_hook is None:
            return
        try:
            self._alert_hook(event, details)
        except Exception:  # noqa: BLE001 — alerting must never break execution
            log.exception("alert hook raised on %s", event)

    def submit(
        self,
        *,
        signal: Signal,
        decision: Decision,
        risk_decision_id: int,
        entry_price: Decimal,
    ) -> ExecutionResult:
        """Place a bracket order for a single approved signal.

        Idempotency contract: if a row already exists with the derived
        `client_order_id`, we DO NOT re-submit. The broker's own dedup on
        `client_order_id` is the safety net if this check ever races.
        """
        if not decision.approved:
            return ExecutionResult(False, "", reason="signal_not_approved")
        if signal.tick_id is None:
            raise ValueError("signal.tick_id is required for idempotent client_order_id")
        if decision.sized_qty <= 0:
            return ExecutionResult(False, "", reason="zero_qty")

        coi = client_order_id_for(signal.tick_id, signal.symbol)
        prior = get_order_by_client_id(self._db, coi)
        if prior is not None:
            log.info("dedup: order %s already submitted (status=%s)", coi, prior.status)
            return ExecutionResult(
                submitted=False,
                client_order_id=coi,
                broker_order_id=prior.broker_order_id,
                reason="idempotent_dedup",
                details={"existing_status": prior.status},
            )

        stop_pct, tp_pct = execution_overrides(signal.strategy)
        stop_price, tp_price = compute_bracket_prices(
            entry_price, stop_pct=stop_pct, take_profit_pct=tp_pct
        )

        # Build bracket request
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import (
            MarketOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        side = OrderSide.BUY if signal.side == "buy" else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=signal.symbol,
            qty=int(decision.sized_qty),
            side=side,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            client_order_id=coi,
            take_profit=TakeProfitRequest(limit_price=float(tp_price)),
            stop_loss=StopLossRequest(stop_price=float(stop_price)),
        )

        client = self._get_client()
        try:
            resp = client.submit_order(order_data=req)
        except Exception as exc:  # noqa: BLE001 — broker error body is the signal here
            body = getattr(exc, "response", None)
            details = {
                "error": str(exc),
                "body": getattr(body, "text", None) or getattr(body, "content", None),
                "symbol": signal.symbol,
                "qty": decision.sized_qty,
                "client_order_id": coi,
            }
            log.error("broker rejected order %s: %s", coi, details)
            # Persist the rejection so we can audit it — NO RETRY.
            insert_order(
                self._db,
                risk_decision_id=risk_decision_id,
                client_order_id=coi,
                broker_order_id=None,
                symbol=signal.symbol,
                side=signal.side,
                qty=decision.sized_qty,
                order_type="bracket",
                entry_price=float(entry_price),
                stop_price=float(stop_price),
                take_profit_price=float(tp_price),
                status="rejected",
                raw_response={"error": str(exc)},
            )
            self._alert("broker_rejection", details)
            return ExecutionResult(False, coi, reason="broker_rejection", details=details)

        broker_id = str(getattr(resp, "id", "") or "")
        status = str(getattr(resp, "status", "") or "new")
        insert_order(
            self._db,
            risk_decision_id=risk_decision_id,
            client_order_id=coi,
            broker_order_id=broker_id,
            symbol=signal.symbol,
            side=signal.side,
            qty=decision.sized_qty,
            order_type="bracket",
            entry_price=float(entry_price),
            stop_price=float(stop_price),
            take_profit_price=float(tp_price),
            status=status,
            raw_response=_serialize_response(resp),
        )
        log.info(
            "submitted bracket %s %s qty=%d entry≈%s stop=%s tp=%s (broker_id=%s)",
            signal.side, signal.symbol, decision.sized_qty,
            entry_price, stop_price, tp_price, broker_id,
        )
        return ExecutionResult(True, coi, broker_order_id=broker_id, reason=status)


def _serialize_response(resp: Any) -> dict[str, Any]:
    # alpaca-py returns pydantic models → model_dump() is the safest json-friendly path.
    for attr in ("model_dump", "dict"):
        fn = getattr(resp, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                pass
    return {"repr": repr(resp)}
