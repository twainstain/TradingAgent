# Execution Status

Mirror of `docs/EXECUTION_PLAN.md` checkboxes. Update this file as work progresses; commit alongside the code that ticks a box. The plan is the spec; this file is the state.

**Last updated:** 2026-04-23
**Current phase:** Phase 5 (orchestrator + LLM judge + structlog + alerts + daily summary landed; one full market day end-to-end is the last exit criterion)
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
- [x] `src/execution/agent.py` — `ExecutionAgent` submits `OrderClass.BRACKET` via alpaca-py; entry + stop + TP in ONE atomic request (no naked entries, invariant #8)
- [x] Stop −2%, TP +4% defaults with per-strategy override via `strategies.execution_overrides(name)` reading `config/strategies.yaml`
- [x] Idempotent `client_order_id = "{tick_id}:{symbol}"` — `ExecutionAgent.submit` checks DB for the id, skips if present; broker-side dedup is the safety net
- [x] `src/execution/fill_poller.py` — per-tick poll of `get_order_by_client_id` for every non-terminal order; upserts `fills`; updates `orders.status`; optional alert hook fires on `filled`
- [x] `src/storage/order_repo.py` + `src/storage/fill_repo.py` (append-only fill timeline, dedup on unchanged (status, qty))
- [x] Rejection path — broker exception → `insert_order(status='rejected', raw_response=…)` + alert_hook(`broker_rejection`); **no automatic retry**; next submit for same id returns `idempotent_dedup`

**Exit criteria**
- [ ] Manual paper: tiny signal → bracket visible in Alpaca UI with stop + TP   <!-- code path fully tested; to run end-to-end, wire orchestrator (Phase 5) or `scripts/` one-shot -->
- [x] `tests/test_execution.py` passes — **12 tests**: bracket prices (defaults + 2dp rounding + strategy override from yaml), happy-path submit (order row carries stop+TP+type='bracket'), bracket request object carries BOTH legs, idempotent dedup hits DB not broker, client_order_id format, **rejection persisted + alert fired + second submit dedups = no retry**, not-approved + zero-qty skip paths, fill poller (partial→filled transitions, unchanged-state dedup, skip-terminal)

**Phase 4 notes**
- Rejection → dedup semantics: a rejected row STILL populates the idempotency key. That means a legit broker rejection (insufficient BP, halted, PDT) isn't silently retried next tick — operator must intervene. This is what the plan wants (invariant: no silent retries).
- Fill poller is append-only so the `fills` table becomes a state-transition log; the latest row is `ORDER BY id DESC LIMIT 1`. Good for audit, bad for naïve SELECTs — Phase 5 daily summary should use windowed aggregation.
- `alert_hook` on ExecutionAgent / fill_poller is a plain callable for now; Phase 5 substitutes `trading_platform.alerting.AlertDispatcher`.
- Full suite: **87 passed**.

---

## Phase 5 — Orchestrator + logging + LLM judgment

**Deliverables**
- [x] `src/orchestrator/main.py` asyncio `Orchestrator.run()` — data → strategy → LLM judge → risk → execute → fill poll, marks at each boundary; calendar gate on every iteration; sleeps to next tick instant
- [x] `src/orchestrator/calendar.py` — `NYSECalendar.in_window(now)` gates ticks; `next_tick()` advances by 5 min or jumps to next session on holidays/weekends
- [x] Tick cadence 5 min, 09:45 → close-minus-15min ET (half-day auto-shifts via `market_close`)
- [x] Per-tick `LatencyTracker` marks: `signals_generated_ms`, `llm_judged_ms`, `orders_sent_ms`, `fills_polled_ms`, plus `indicators_ms`/`snapshot_write_ms`/`retention_ms` from DataAgent
- [x] `src/llm/judge.py` — Haiku 4.5 with **prompt-cached system + indicators schema** (anthropic `system=[{...cache_control: ephemeral}]`); returns `{"approve","reason","confidence"}`; falls back to rule-only on cap hit / API outage / parse failure
- [x] `src/llm/cost_tracker.py` — `DailyCostTracker.is_over_cap(td)` reads `llm_calls` SUM(cost_usd); hard cap defaults to `$MAX_LLM_DAILY_USD=5`
- [x] `src/storage/llm_call_repo.py` — every call writes prompt_hash, full prompt, response, tokens_in/out, cost_usd, cache_hit, latency_ms, model
- [x] `src/orchestrator/logging.py` — structlog → `logs/YYYY-MM-DD.jsonl`; redactor strips `_token`/`_secret`/`_password`/`_api_key` suffixes + full Anthropic `sk-ant-…` / Alpaca `PK…` patterns
- [x] `src/orchestrator/daily_summary.py` — `write_summary(db, date)` renders `logs/daily_summary_YYYY-MM-DD.md` (fill count, signal branch counts, LLM cost+cache hits, p50/p95 from latency.jsonl, top 20 fills)
- [x] `trading_platform.alerting.AlertDispatcher` — `src/orchestrator/alerts.py` builds dispatcher from env (Gmail + Telegram backends, each self-skips if unconfigured); `hook_from(d)` adapts to the `(event, details)` shape that execution/risk already use
- [x] `signals.llm_branch` column populated — `rule_only` / `llm_approved` / `llm_rejected`; orchestrator updates the row after judgment

**Exit criteria**
- [ ] One full market day end-to-end, zero unhandled exceptions   <!-- requires live run during market hours -->
- [x] Daily summary writes on that day — covered by `test_daily_summary_renders_and_writes` + `test_daily_summary_latency_percentiles`
- [x] Latency marks produced — validated in Phase 1 DataAgent tests + new orchestrator marks are wired in `main.py` (structural)
- [x] LLM cost cap triggers correctly — `test_judge_skips_when_cap_hit` seeds `llm_calls` with cost=$5, uses a client that raises if called; judge returns `skipped=True, branch='rule_only'`
- [x] Calendar holiday → zero ticks — `test_mlk_day_holiday_out_of_window` + `test_next_tick_on_holiday_jumps_to_next_session`

**Phase 5 notes**
- **FLATTEN_ALL** on kill-switch with open positions is emitted as an *alert* (to the operator) in Phase 5, not an automatic broker close-out. The dashboard's kill-switch button (Phase 5b) is the primary write path; the operator acts on the alert. This keeps the execution side of the kill switch human-in-the-loop even when the rule side is automatic.
- Live gate: `_confirm_live_gate()` in `main.py` blocks boot with `input("… type YES …")` when `LIVE=1`. Paper is default; no way to flip mode non-interactively.
- Daily summary scheduling: the orchestrator's main loop calls `_maybe_write_summary` each iteration (idempotent, one write per trading date) at/after 16:30 ET. No cron needed.
- Prompt-caching: system prompt + indicators schema are stable text marked `cache_control: ephemeral`. Per-call user content (signal + snapshot + headlines) is not cached. Subsequent calls in the same session get cached-input pricing (~1/10 of uncached) — `DailyCostTracker` reads the actual `cache_read_input_tokens` from Anthropic's response, so cost math stays honest.
- Full suite: **109 passed**.

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
