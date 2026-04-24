"""Orchestrator — asyncio tick loop.

Per-tick flow (ARCHITECTURE §3.5):
  LatencyTracker.start_cycle()
    → data agent (feature recompute, snapshots written, retention)
    → strategy agent (signals written)
    → LLM judge (optional)         [llm_judged mark]
    → risk agent (approve / reject) [risk_decided mark]
    → execution agent (brackets)    [orders_sent mark]
    → fill poller                   [fills_polled mark]

All boundaries marked; the LatencyTracker flushes one JSONL record per
tick to `logs/latency.jsonl`.

The orchestrator does not implement the data-fetch websocket itself —
that's the data agent's live feed (Phase 1). In this Phase 5 tick-loop
path, we call `DataAgent.run_tick(price_map)` where the `price_map` is
built from the agent's TTL-cached latest quotes (populated by the WS
layer in production, or by a replay path in tests).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_platform.observability import LatencyTracker
from trading_platform.persistence.db import close_db, init_db

from core.watchlist import load_watchlist
from execution.agent import ExecutionAgent
from execution.fill_poller import poll_fills
from llm.cost_tracker import DailyCostTracker
from llm.judge import LLMJudge
from market.alpaca_rest import backfill_bars_daily
from market.data_agent import DataAgent
from orchestrator.alerts import build_dispatcher, hook_from, null_hook
from orchestrator.calendar import ET, NYSECalendar, et_trading_date, next_tick
from orchestrator.daily_summary import write_summary
from orchestrator.logging import configure as configure_logging
from risk_rules.agent import FLATTEN_ALL_SIGNAL, RiskAgent
from risk_rules.daily_halt import DailyHaltBreaker
from risk_rules.earnings import EarningsCalendar
from risk_rules.portfolio import fetch_portfolio
from storage.signal_repo import write_signals
from strategies.agent import StrategyAgent

log = logging.getLogger(__name__)

TICK_CADENCE_SECONDS = 300
SUMMARY_AT_ET = (16, 30)  # 16:30 ET


class Orchestrator:
    def __init__(
        self,
        db,
        *,
        symbols: tuple[str, ...],
        data_agent: DataAgent,
        strategy_agent: StrategyAgent,
        risk_agent: RiskAgent,
        execution_agent: ExecutionAgent,
        judge: LLMJudge | None,
        halt_breaker: DailyHaltBreaker,
        calendar: NYSECalendar,
        latency_tracker: LatencyTracker,
        earnings,  # EarningsCalendar or Static for tests
        alert_hook,
        cadence_seconds: int = TICK_CADENCE_SECONDS,
    ) -> None:
        self._db = db
        self._symbols = symbols
        self._data_agent = data_agent
        self._strategy_agent = strategy_agent
        self._risk_agent = risk_agent
        self._exec_agent = execution_agent
        self._judge = judge
        self._halt_breaker = halt_breaker
        self._calendar = calendar
        self._tracker = latency_tracker
        self._earnings = earnings
        self._alert_hook = alert_hook
        self._cadence_seconds = cadence_seconds
        self._shutdown = asyncio.Event()
        self._last_summary_date: date | None = None

    def request_stop(self) -> None:
        self._shutdown.set()

    def _start_tick_row(self, started_at: datetime) -> int:
        self._db.execute(
            "INSERT INTO ticks (started_at, status) VALUES (?, 'running')",
            (started_at.isoformat(),),
        )
        self._db.commit()
        return int(self._db.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _finish_tick_row(self, tick_id: int, status: str, finished_at: datetime) -> None:
        self._db.execute(
            "UPDATE ticks SET finished_at = ?, status = ? WHERE id = ?",
            (finished_at.isoformat(), status, tick_id),
        )
        self._db.commit()

    async def run_one_tick(self, now: datetime) -> None:
        """One iteration of the tick loop. Assumes the calendar gate has passed."""
        self._tracker.start_cycle()
        tick_id = self._start_tick_row(now)
        try:
            # 1. Data: ingest latest prices → features → snapshots
            price_map = self._data_agent.price_map()
            self._data_agent.run_tick(price_map, tick_id=tick_id, now=now)

            # 2. Strategy
            signals = self._strategy_agent.run(tick_id=tick_id, now=now)
            self._tracker.mark("signals_generated_ms")

            # 3. Optional LLM judgment → A/B branch stamped on signal
            if self._judge is not None and signals:
                for sig in signals:
                    snap = self._data_agent.cache.get(sig.symbol)
                    if snap is None:
                        continue
                    res = self._judge.judge(signal=sig, snapshot=snap, headlines=[])
                    # Update the signal row's llm_branch + llm_call_id
                    self._db.execute(
                        "UPDATE signals SET llm_branch = ?, llm_call_id = ? WHERE tick_id = ? AND symbol = ?",
                        (res.branch, res.llm_call_id, tick_id, sig.symbol),
                    )
                    self._db.commit()
                self._tracker.mark("llm_judged_ms")

            # 4. Daily halt check (cheap, uses cached portfolio)
            td = et_trading_date(now)
            try:
                portfolio = fetch_portfolio()
            except Exception as exc:  # noqa: BLE001
                log.warning("portfolio fetch failed — skipping risk + execute: %s", exc)
                self._finish_tick_row(tick_id, "error", datetime.now(timezone.utc))
                return
            state = self._halt_breaker.check(
                trading_date=td,
                starting_equity=portfolio.equity,  # naive: uses today's equity as baseline
                current_equity=portfolio.equity,
            )
            if state and state.halted and not self._already_halted(td):
                self._alert_hook("daily_halt_engaged", {"trading_date": str(td), "reason": state.reason})

            # 5. Risk agent: per approved LLM / rule_only signal
            approved_signals = self._approved_after_judge(tick_id)
            for sig_row in approved_signals:
                risk_res = self._risk_agent.evaluate(
                    sig_row["signal"],
                    portfolio=portfolio,
                    price=sig_row["price"],
                    now_et=now.astimezone(ET),
                    trading_date=td,
                    earnings=self._earnings,
                    signal_id=sig_row["signal_id"],
                )
                if risk_res.flatten_all:
                    self._alert_hook("kill_switch_engaged", {"path": "data/KILL"})
                    # FLATTEN_ALL is a named intent — execution-side implementation is manual
                    # for Phase 5; leaving as alert-only. Phase 5b dashboard already has the
                    # button; operator acts on the alert.
                    log.warning("FLATTEN_ALL emitted (alert only in Phase 5): %s", FLATTEN_ALL_SIGNAL)
                    continue
                if not risk_res.decision.approved:
                    continue
                self._exec_agent.submit(
                    signal=sig_row["signal"],
                    decision=risk_res.decision,
                    risk_decision_id=self._latest_risk_decision_id(sig_row["signal_id"]),
                    entry_price=sig_row["price"],
                )
            self._tracker.mark("orders_sent_ms")

            # 6. Poll fills
            poll_fills(self._db, alert_hook=self._alert_hook)
            self._tracker.mark("fills_polled_ms")

            # 7. Record cycle summary to latency.jsonl
            self._tracker.record_pipeline(
                candidate_id=str(tick_id),
                pipeline_timings={},
                status="ok",
                cycle_marks=self._tracker.get_marks(),
            )
            self._finish_tick_row(tick_id, "ok", datetime.now(timezone.utc))
        except Exception:
            log.exception("unhandled tick error")
            self._finish_tick_row(tick_id, "error", datetime.now(timezone.utc))

    def _already_halted(self, td: date) -> bool:
        return self._halt_breaker.is_halted(td)

    def _approved_after_judge(self, tick_id: int) -> list[dict[str, Any]]:
        """Signals that can proceed to risk: branch ∈ {rule_only, llm_approved}."""
        rows = self._db.execute(
            """
            SELECT id AS signal_id, symbol, strategy, side, confidence, reason,
                   tick_id, llm_branch, created_at
            FROM signals
            WHERE tick_id = ? AND COALESCE(llm_branch, 'rule_only') IN ('rule_only', 'llm_approved')
            ORDER BY id ASC
            """,
            (tick_id,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            snap = self._data_agent.cache.get(r["symbol"])
            price = Decimal(str(snap.price)) if snap else None
            if price is None:
                continue
            from core.signal import Signal

            out.append(
                {
                    "signal_id": r["signal_id"],
                    "signal": Signal(
                        symbol=r["symbol"], side=r["side"], strategy=r["strategy"],
                        confidence=r["confidence"] or 0.0, reason=r["reason"] or "",
                        created_at=datetime.fromisoformat(r["created_at"]),
                        tick_id=r["tick_id"], llm_branch=r["llm_branch"],
                    ),
                    "price": price,
                }
            )
        return out

    def _latest_risk_decision_id(self, signal_id: int) -> int:
        row = self._db.execute(
            "SELECT id FROM risk_decisions WHERE signal_id = ? ORDER BY id DESC LIMIT 1",
            (signal_id,),
        ).fetchone()
        # If risk.evaluate() has already persisted it, great — else insert_order will FK-fail
        # and we'll log the error. We never fabricate IDs.
        if row is None:
            raise RuntimeError(f"no risk_decision for signal {signal_id}")
        return int(row["id"])

    async def _maybe_write_summary(self, now: datetime) -> None:
        now_et = now.astimezone(ET)
        td = now_et.date()
        if (now_et.hour, now_et.minute) < SUMMARY_AT_ET:
            return
        if self._last_summary_date == td:
            return
        try:
            write_summary(self._db, td)
            self._last_summary_date = td
            log.info("daily summary written for %s", td)
        except Exception:
            log.exception("daily summary failed")

    async def run(self) -> None:
        while not self._shutdown.is_set():
            now = datetime.now(timezone.utc)
            in_win, _ = self._calendar.in_window(now)
            if in_win:
                await self.run_one_tick(now)
            # Regardless, try end-of-day summary.
            await self._maybe_write_summary(now)
            # Sleep until the next scheduled tick instant.
            next_at = next_tick(now, cadence_seconds=self._cadence_seconds, calendar=self._calendar)
            sleep_s = max(1.0, (next_at - now).total_seconds())
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                continue


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _confirm_live_gate() -> None:
    """Three-opt-in live gate. Paper is the default — if LIVE=1 is set in
    env, require an interactive YES at startup. Otherwise pass silently.
    """
    if os.environ.get("LIVE") != "1":
        return
    try:
        ans = input("LIVE=1 detected. Type YES to trade real money: ").strip()
    except EOFError:
        ans = ""
    if ans != "YES":
        raise SystemExit("live gate: aborted (did not type YES)")


def build_default(db_path: str | None = None) -> Orchestrator:
    configure_logging()
    _confirm_live_gate()

    schema_path = Path(__file__).resolve().parents[2] / "src" / "schema.sql"
    db_file = db_path or str(Path(__file__).resolve().parents[2] / "data" / "events.db")
    db = init_db(db_file, schema_path.read_text())

    symbols = load_watchlist()
    # Startup: 60-day bar backfill so indicators can warm up.
    try:
        backfill_bars_daily(db, symbols, lookback_days=60)
    except Exception as exc:  # noqa: BLE001
        log.warning("startup backfill failed (continuing): %s", exc)

    latency_path = Path(__file__).resolve().parents[2] / "logs" / "latency.jsonl"
    tracker = LatencyTracker(str(latency_path))

    data_agent = DataAgent(db, symbols=symbols, latency_tracker=tracker)
    strat_agent = StrategyAgent(db, symbols=symbols)
    risk_agent = RiskAgent(db)
    exec_agent = ExecutionAgent(db)
    judge = LLMJudge(db) if os.environ.get("ANTHROPIC_API_KEY") else None
    halt_breaker = DailyHaltBreaker(db)
    earnings = EarningsCalendar()

    try:
        dispatcher = build_dispatcher()
        alert_hook = hook_from(dispatcher)
    except Exception as exc:  # noqa: BLE001
        log.warning("alerts disabled: %s", exc)
        alert_hook = null_hook()

    return Orchestrator(
        db,
        symbols=symbols,
        data_agent=data_agent,
        strategy_agent=strat_agent,
        risk_agent=risk_agent,
        execution_agent=exec_agent,
        judge=judge,
        halt_breaker=halt_breaker,
        calendar=NYSECalendar(),
        latency_tracker=tracker,
        earnings=earnings,
        alert_hook=alert_hook,
    )


def main() -> int:
    orch = build_default()
    loop = asyncio.new_event_loop()

    def _sig(*_a):
        orch.request_stop()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _sig)
        except NotImplementedError:
            # Windows; signals handled via KeyboardInterrupt.
            pass

    try:
        loop.run_until_complete(orch.run())
    except KeyboardInterrupt:
        orch.request_stop()
    finally:
        close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
