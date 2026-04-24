# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

This is a **greenfield project**. As of now the repo contains only `docs/ARCHITECTURE.md` and `docs/EXECUTION_PLAN.md` — no source, no `pyproject.toml`, no Dockerfile, no tests. Every future session will likely be implementing one of the phases defined in `docs/EXECUTION_PLAN.md`.

When adding code, follow the file layout implied by the architecture doc:

```
data_agent.py          strategies/mean_reversion.py
strategy_agent.py      strategies/momentum.py
risk_agent.py          judge.py           # LLM judgment stage
execution_agent.py     main.py            # asyncio orchestrator
```

with persistence in `data/events.db` (SQLite) and `logs/YYYY-MM-DD.jsonl`.

## Phased execution — respect the gate

`docs/EXECUTION_PLAN.md` defines 8 phases (0 = skeleton through 8 = live readiness). **Do not skip exit criteria.** Each phase has explicit "must be true to move on" checks; most retail trading projects fail by rushing from "it runs" to "it trades real money." If the user asks to implement work that belongs to a later phase before earlier exit criteria are met, flag it. after every task is done update src/memory/status.md file for saving the context for future.

Current expected sequence:
- **Phase 0**: repo skeleton, `.env.example`, `docker-compose.yml`, `Dockerfile`, both services start, `hello.py` pings Alpaca + Anthropic.
- **Phase 1**: data agent → Redis snapshots (5-min TTL).
- **Phase 2**: rule-only strategies, signals logged, no orders yet.
- **Phase 3**: risk agent + kill switch. Unit tests per rule are required for the exit criteria.
- **Phase 4**: execution agent with **bracket orders only** (entry + stop + TP).
- **Phase 5**: orchestrator + LLM judgment + structlog + daily summary + LLM cost cap.
- **Phase 6**: 4-week paper soak, no more than one param change per week.
- **Phase 8**: live readiness — gated on all checklist boxes plus interactive startup confirmation.

## Non-negotiable architectural invariants

These are enforced in code, not prompts. Don't rewrite these rules in a way that weakens them.

1. **The LLM cannot override risk.** Risk Agent is plain Python with no model calls. Strategy/judgment signals flow *into* Risk; approved signals flow *out*. Both approvals and rejections are logged.
2. **Strategy never sends orders.** It returns `Signal` objects. Only the Execution Agent calls the broker, and only on `Approved(signal, sized_qty)` from Risk.
3. **Paper endpoint by default.** Live endpoint requires `LIVE=1` env flag **and** an interactive "type YES" prompt at startup. No auto-enable, ever.
4. **Kill switch is a local file** (`/app/KILL` or `data/KILL`). Not an API, not a remote flag — presence of the file → reject all signals + emit `FLATTEN_ALL`. This is deliberate: no network can flip it.
5. **Every order is traceable.** Order row → risk_decision_id → signal_id → tick_id → llm_call_id (if any). If a change breaks this chain, it's wrong.
6. **Stale data = abstain.** Quotes older than ~90s must not drive orders. Websocket disconnects → skip ticks, don't trade on last-known values.
7. **Daily loss halt is sticky.** The `halted:YYYY-MM-DD` flag lives in Redis and must survive a process restart on the same trading day. Only the date rollover clears it.
8. **Bracket orders, always.** Entry + stop-loss + take-profit, attached atomically. No naked entries.
9. **Idempotent orders.** `client_order_id = {tick_id}:{symbol}`. Broker-side dedup is the safety net.

## Model selection

- **Haiku 4.5** is the default for the judgment stage — the cost math in the plan assumes it.
- **Sonnet 4.6** only for specific ambiguous-rule cases (see Architecture §3.2) and, per Phase 7, potentially on the final decision *only* if judgment has earned its keep in the soak.
- The system prompt + indicators schema must be **prompt-cached**. LLM cost has a hard daily cap (`MAX_LLM_DAILY_USD`, default $5) — above it, fall back to rule-only.
- Every LLM call logs: prompt, response, tokens in/out, cost, cache hit/miss. Non-negotiable; this is how we answer "was the LLM worth it?" at Phase 6 review.

## Commands (once Phase 0 is implemented)

Phase 0 will introduce:
- `docker compose up` — brings up `agent` + `redis`.
- `pytest` — unit tests (Phase 3 requires a test per risk rule).
- A `hello.py` smoke script inside the container.

There is no lint/test/build command to document yet. When implementing Phase 0, prefer `pyproject.toml` over `requirements.txt` and add commands here.

## Observability expectations

Per tick the event log captures: tick_id, watchlist, features (count + NaN count), signals, risk decisions with reasons, orders, fills, and LLM calls with cost. A daily summary job at 16:30 ET writes `logs/daily_summary_YYYY-MM-DD.md`. Do not add new decision surfaces without wiring them into both SQLite and the JSONL stream.

## Secrets & deployment

- `.env` is gitignored; `.env.example` with blank keys is committed. Expected keys: Alpaca key/secret, Anthropic, Polygon.
- Container runs as non-root.
- `structlog` must redact keys from log output.
- Paper-phase host target: local Mac or $5–10/mo VPS. Live-phase target: AWS us-east-1 (Alpaca's region).

## Rules
- Use minimal context
- Limit reasoning depth
- Output concise answers
- Avoid repetition
- No unnecessary loops

## Execution Principle
Do the minimum required work to produce a correct result.

## Conventions enforced across the codebase

- **Decimal, never float.** `src/core/models.py::_coerce_decimals` auto-converts int/float passed to frozen dataclasses. `BPS_DIVISOR = Decimal("10000")` for bps math.
- **Dataclasses are frozen.** Rewrites happen through constructors, not attribute mutation.
- **Paper is the default.** Never flip a default to live. Three-opt-in live gate is non-negotiable.
- **Strategy defaults may not be tuned speculatively.** `min_fill_price`, `move_threshold_bps`, buffer dicts — these are calibrated against measurement docs.

## Common commands

All commands assume Python 3.11 and `PYTHONPATH=src`. The repo uses `pip install -e .[dev]` (though `pyproject.toml` itself isn't currently tracked — `tests/conftest.py` wires `src/`, `lib/trading_platform/src/`, and `scripts/` onto `sys.path` so `pytest tests/` works without install).

# Validation

Before any commit:

1. Run tests:
   python -m pytest tests/ -q

2. Run simulation:
   PYTHONPATH=src python -m main --config config/example_config.json --iterations 5

3. Verify:
- no regressions
- correct trade filtering
- stable execution
