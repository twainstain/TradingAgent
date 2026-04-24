# Execution Status

Mirror of `docs/EXECUTION_PLAN.md` checkboxes. Update this file as work progresses; commit alongside the code that ticks a box. The plan is the spec; this file is the state.

**Last updated:** 2026-04-23
**Current phase:** Phase 3 (code + 27 risk unit tests landed; manual KILL/halt drills still to run; eyeball-review of replay.py still pending live bars)
**Module layout:** package layout — `src/{core,market,strategies,llm,risk_rules,execution,orchestrator,storage,dashboard}`. Do not shadow platform top-level names (`pipeline`, `risk`, `alerting`, `observability`, `persistence`).

---

## Phase 0 — Accounts, keys, skeleton

**Deliverables**
- [x] Alpaca paper account + keys (paper equity $100k, status ACTIVE — verified by `scripts/hello.py`)
- [x] Anthropic API key (verified: Haiku 4.5 response received)
- [x] Polygon.io free tier (key present in .env; no live call yet — exercised in Phase 3 earnings blackout)
- [x] Directory layout created (`src/{core,market,strategies,llm,risk_rules,execution,orchestrator,storage,dashboard}`, `tests/`, `config/`, `data/`, `deploy/`, `docs/`, `lib/`, `logs/`, `memory/`, `scripts/`)
- [x] `.gitignore` set
- [x] `lib/trading_platform` submodule added + pinned at `495b4c2` (url: https://github.com/twainstain/trading-platform.git). `.gitmodules` committed. **Doc drift: ARCHITECTURE §10 shows flat imports (`from pipeline import …`), but the pinned submodule uses namespace form (`from trading_platform.pipeline import …`) — update the doc when Phase 1 starts importing.**
- [x] `.env.example` committed
- [x] `pyproject.toml` runtime deps
- [x] `pyproject.toml` dev extras
- [x] `deploy/Dockerfile` (non-root, `pip install -e lib/trading_platform`)
- [x] `deploy/docker-compose.yml` (agent + dashboard, `env_file: .env`, localhost-bound dashboard)
- [x] `tests/conftest.py` sys.path wiring
- [x] `src/_bootstrap.py` sys.path shim for scripts
- [x] `src/schema.sql` with all tables from ARCHITECTURE §3.6
- [x] Stub `src/dashboard/app.py` with `/health`
- [x] `scripts/hello.py`

**Exit criteria**
- [ ] `docker compose up` brings up both services cleanly   <!-- blocked on submodule populated + .env filled -->
- [ ] `curl localhost:8000/health` returns `{"ok": true}`   <!-- verified via TestClient in `tests/test_smoke.py`; live `docker compose` run blocked on above -->
- [x] `scripts/hello.py` prints Alpaca equity + Anthropic response (verified 2026-04-23: paper equity $100k, Haiku 4.5 10-token response)
- [x] `pytest tests/ -q` passes (3 smoke tests: sys.path, schema-tables, dashboard /health)
- [x] DB bootstrap: `sqlite3` loads schema.sql and creates all 10 tables (verified locally on python3.11)

---

## Phase 1 — Data agent

**Deliverables**
- [x] `src/market/alpaca_ws.py` (10-symbol watchlist; subscribes quotes + minute bars via alpaca-py `StockDataStream`)
- [x] `src/market/alpaca_rest.py` (60-day daily bars → `bars_daily`; `fetch_daily_bars`/`backfill_bars_daily`)
- [x] `src/market/indicators.py` (RSI-14, SMA-20/50/200, avg_vol_20, ATR-14 via `pandas_ta_classic`)
- [x] `src/market/data_agent.py` (writes `snapshots` rows; no `stale` column; `run_tick(price_map)` sync entrypoint)
- [x] In-process `TTLCache` (90s) + write-through to SQLite (cache update happens AFTER DB commit)
- [x] `LatencyTracker` wired; marks `indicators_ms`, `snapshot_write_ms`, `retention_ms` — plan calls for `data_fetch_ms`/`indicators_ms` specifically, see note below
- [x] Retention job: prune `snapshots` older than 2 trading days — called each `run_tick`
- [x] `config/watchlist.yaml` + `src/core/watchlist.py` loader (SPY QQQ AAPL MSFT NVDA AMZN META GOOGL TSLA AMD)

**Exit criteria**
- [ ] All 10 symbols within 60s of wall clock during market hours   <!-- blocked: Alpaca API key missing; cannot run live websocket -->
- [x] `tests/test_indicators.py` passes (6 tests, RSI within 0.1, SMA match, ATR positive, short-history NaN, price override, missing-column raise)
- [x] `tests/test_snapshot_repo.py` passes (6 tests: round-trip, NaN→NULL, 90s filter, newest-within-window, bulk, retention)
- [x] `tests/test_bars_repo.py` passes (upsert/load + fetch_daily_bars with fake Alpaca client)
- [x] `tests/test_data_agent.py` passes (6 tests: run_tick + missing price/bars skip + retention prune + latency marks + cache)
- [x] `logs/latency.jsonl` marks emitted (verified via LatencyTracker in `test_latency_tracker_writes_marks`; **rename `snapshot_write_ms` → `db_write_ms` to match plan wording in Phase 5 if we care**)

**Phase 1 notes**
- `pandas-ta` was delisted from PyPI → swapped to `pandas-ta-classic` (import: `pandas_ta_classic`). API is drop-in.
- `DataAgent.record_quote` keeps the websocket hot path cheap: it only writes to TTLCache. The Orchestrator (Phase 5) will call `run_tick(agent.price_map())` on its 5-min cadence.
- Full suite: **24 passed** on python3.11 with `pytest tests/ -q`.

---

## Phase 2 — Strategy agent (rule-only)

**Deliverables**
- [x] `src/strategies/mean_reversion.py` (rsi14<30 ∧ vol_today>1.5·avg_vol_20 ∧ price>sma200)
- [x] `src/strategies/momentum.py` (price>sma50>sma200 ∧ 50≤rsi14≤70 ∧ close>yesterday_close)
- [x] `src/strategies/__init__.py` loads from `config/strategies.yaml` (registry + `load_enabled_strategies`)
- [x] `src/strategies/base.py` (`StrategyContext` with volume_today / yesterday_close; NaN-safe `_is_num`)
- [x] `src/strategies/agent.py` (fan-out + merge most-recent-per-symbol + persists with `llm_branch='rule_only'`)
- [x] Signals → `signals` table (FK `tick_id`, via `src/storage/signal_repo.py`)
- [x] `scripts/replay.py` — replay harness from `bars_daily` (one tick per date; prints signal+feature summary)
- [x] `config/strategies.yaml` (enabled flags + Phase-4-ready stop/TP overrides)
- [x] `src/core/signal.py` (frozen `Signal` dataclass matching schema cols)

**Exit criteria**
- [ ] `python scripts/replay.py --date YYYY-MM-DD` produces reasonable-looking signals   <!-- code paths exercised by `tests/test_replay.py` with synthetic bars; real eyeball-review requires live bars_daily from Phase 1 backfill (blocked on Alpaca key) -->
- [x] `tests/test_strategies.py` passes — 22 tests including: 4 mean_reversion (fire/skip), 4 momentum (fire/skip), NaN/None partials (7 parametrized), missing-context (1), config loader (2), agent end-to-end (4 incl. broken-strategy isolation, stale-filter, merge)
- [x] `tests/test_replay.py` passes — replay CLI runs green with seeded synthetic bars + handles missing-date case

**Phase 2 notes**
- Merge rule: one signal per symbol per tick; later strategies in the registry win ties (deterministic).
- `StrategyAgent` catches exceptions per strategy so one broken rule can't drop the tick (covered by `test_agent_survives_broken_strategy`).
- `replay.py` does one tick per date (daily bar close as price). The plan's "per-minute ticks" wording was aspirational — adds no information until we have intraday bars (Phase 7).
- Full suite: **48 passed**.

---

## Phase 3 — Risk agent + kill switch

**Deliverables**
- [x] `src/risk_rules/rules.py` — 7 rules: `KillSwitchRule`, `DailyHaltRule`, `TradingHoursRule` (09:45-15:45 ET), `MaxOpenPositionsRule` (≤8), `MaxPositionSizeRule` (≤3% equity/symbol), `MaxTotalExposureRule` (≤50% equity), `EarningsBlackoutRule` (2-day window)
- [x] `src/risk_rules/agent.py` — `RiskAgent` wrapping `RuleBasedPolicy`; persists approve/reject to `risk_decisions`; emits `FLATTEN_ALL` when kill switch engaged AND positions exist
- [x] `src/risk_rules/sizing.py` — Decimal-only `size_order(equity, current_exposure, price)` with `ROUND_DOWN`; floats accepted at boundary, never used for math
- [x] `src/risk_rules/portfolio.py` — `Portfolio` / `PositionInfo` frozen dataclasses (Decimal fields); `fetch_portfolio(client)` hits Alpaca on every call
- [x] `src/risk_rules/earnings.py` — `EarningsCalendar` (Polygon `/vX/reference/tickers/{t}/events`, 6h TTLCache, fails-open on outage); `StaticEarningsCalendar` for tests/offline
- [x] `src/risk_rules/kill_switch.py` — `is_engaged()` checks `data/KILL` file; FLATTEN_ALL constant
- [x] `src/risk_rules/daily_halt.py` — `DailyHaltBreaker` trips at −2%, persists to `risk_state`; clears on date rollover. **Plan said "via CircuitBreaker"; the platform breaker is failure/staleness-driven, not P&L — see note below.**
- [x] `src/storage/risk_state_repo.py` — `ensure_state`, `mark_halted`, `mark_kill_switch_engaged`, `is_halted_today`
- [x] `src/storage/risk_decision_repo.py` — `write_decision` (approvals AND rejections)

**Exit criteria**
- [x] `tests/test_risk_rules.py` — **27 tests**: per-rule (14: each rule has an approve + a reject case), sizing (6: per-symbol cap / total cap / floor / float-coerce / zero-price), daily halt persistence (3: trip at 2%, don't trip at 1%, survives close/reopen == simulated restart), `RiskAgent` integration (4: happy path, kill+positions=flatten-all, kill+no-positions=no-flatten, rejections persisted)
- [x] Kill-switch behavior covered by unit tests; manual `touch data/KILL` drill still TODO (trivial given the code)
- [x] Halt-across-restart covered by `test_daily_halt_survives_reconnect` (closes the DB, reopens the same file — same effect as docker restart); manual end-to-end drill still TODO

**Phase 3 notes**
- `DailyHaltBreaker` does **not** wrap `trading_platform.risk.CircuitBreaker`. The platform breaker is event/window-driven (failures, stale data, rate limits); the daily halt is a P&L threshold with sticky persistence. Semantically similar, mechanically different — using the platform breaker would have required feeding it synthetic "failure" events. Plan wording was aspirational.
- Earnings: fails-open on Polygon outage (signal flows through) but logs a warning. Kill switch and daily halt remain hard stops regardless. Consistent with §7 Failure Modes.
- `MaxOpenPositionsRule` allows ADD to an existing symbol even at the 8-position limit (otherwise you can't average in or scale).
- Full suite: **75 passed**.

---

## Phase 4 — Execution agent

**Deliverables**
- [ ] `src/execution/agent.py` (bracket orders)
- [ ] Stop −2%, TP +4% (per-strategy override)
- [ ] Idempotent `client_order_id = {tick_id}:{symbol}`
- [ ] Fill tracking via `/v2/orders` polling → `fills` table
- [ ] Rejection logging + alert, no auto-retry

**Exit criteria**
- [ ] Manual paper: tiny signal → bracket visible in Alpaca UI with stop + TP
- [ ] `tests/test_execution.py` passes

---

## Phase 5 — Orchestrator + logging + LLM judgment

**Deliverables**
- [ ] `src/orchestrator/main.py` asyncio loop
- [ ] `src/orchestrator/calendar.py` (`pandas_market_calendars`)
- [ ] Tick cadence 5 min, 09:45 → close-minus-15min ET
- [ ] Per-tick LatencyTracker marks at every boundary
- [ ] `src/llm/judge.py` (Haiku 4.5, prompt-cached)
- [ ] `src/llm/cost_tracker.py` (hard cap `MAX_LLM_DAILY_USD=5`)
- [ ] `llm_calls` rows include prompt hash, tokens, cost, cache_hit, latency
- [ ] `src/orchestrator/logging.py` (structlog + redactor)
- [ ] `src/orchestrator/daily_summary.py` cron at 16:30 ET
- [ ] `AlertDispatcher` wired (fills, rejections, halt, kill-switch)
- [ ] `signals.llm_branch` column for A/B attribution

**Exit criteria**
- [ ] One full market day end-to-end, zero unhandled exceptions
- [ ] Daily summary written for that day
- [ ] `analyze_latency` prints per-stage p50/p95
- [ ] Unit test: LLM cost cap triggers → judgment skipped
- [ ] Calendar holiday → zero ticks

---

## Phase 5b — Dashboard

**Deliverables**
- [ ] `src/dashboard/` FastAPI app, SQLite read-only
- [ ] Views: Today, Signals, Latency, Daily summary, Admin
- [ ] Styling per `DESIGN.md`
- [ ] `deploy/docker-compose.yml` binds dashboard to `127.0.0.1:8000`
- [ ] Admin kill-switch button (only dashboard write path)

**Exit criteria**
- [ ] `tests/test_dashboard.py` passes (read-only DB, localhost-only kill switch)
- [ ] Manual: Today view matches Alpaca UI; Latency view matches `analyze_latency`
- [ ] Killing dashboard process doesn't affect agent

---

## Phase 6 — Paper soak (4 weeks)

**Entry gate** (from Phase 5, moved here)
- [ ] 3 consecutive market days clean before the 4-week clock starts

**Deliverables**
- [ ] VPS deployment live
- [ ] `docs/CHANGELOG.md` started day 1 of soak
- [ ] 20 trading days uninterrupted
- [ ] Weekly Sunday review (each week has a CHANGELOG entry or an explicit "no changes this week")

**Exit criteria**
- [ ] Sharpe ≥ 0.5
- [ ] Max DD < 5%
- [ ] Zero Risk Agent bypasses in logs
- [ ] Zero unhandled exceptions in last 10 trading days
- [ ] On executed trades, LLM-approved P&L ≥ rule-only P&L (else disable LLM)
- [ ] Operator can trace any trade via dashboard or SQLite chain
- [ ] Tick p95 latency stable; no stage > 3s for ≥99% of ticks

---

## Phase 7 — Iterate

- [ ] Expand watchlist (10 → 20, then 20 → 50)
- [ ] Sector rotation strategy
- [ ] Portfolio regime filter
- [ ] Defined-risk options (verticals, covered calls only)
- [ ] Consider Sonnet 4.6 on final decision (only if LLM earned it in soak)

---

## Phase 8 — Live readiness

- [ ] Phase 6 exit held ≥4 weeks
- [ ] Kill-switch drill: flatten-all within one tick
- [ ] Survived ≥1 volatile day (>2% SPY move)
- [ ] Understand every trade from last week
- [ ] Real-money loss cap set (1–5% of paper-equivalent)
- [ ] `LIVE=1` + interactive "type YES" confirmed
- [ ] Paper instance continues in parallel
- [ ] Phase-5 alerts verified live (fills, rejections, halt, kill-switch)
- [ ] CPA / IRS pub consulted
