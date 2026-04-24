# AI Trading Agent — Execution Plan

Each phase has **Deliverables** and **Exit criteria** as checkboxes. Do not skip exit criteria. Most retail trading projects fail because they rush from "it runs" to "I'm live."

**Progress is tracked in `memory/status.md`** — keep it in sync as boxes are ticked.

**Module layout decision (locked for this plan):** `src/` uses the package layout from the src-structure review — `core/`, `market/`, `strategies/`, `llm/`, `risk_rules/`, `execution/`, `orchestrator/`, `storage/`, `dashboard/`. The platform's top-level names (`pipeline`, `risk`, `alerting`, `observability`, `persistence`) are **not** shadowed. If this changes, update both this plan and `memory/status.md` in the same commit.

---

## Phase 0 — Accounts, keys, skeleton (Day 1, ~3 hours)

**Deliverables**
- [ ] Alpaca paper account created; API key + secret in a password manager.
- [ ] Anthropic API key; $10 credit loaded.
- [ ] Polygon.io free tier account.
- [ ] Repo directory layout per `ARCHITECTURE.md §9`: `src/`, `tests/`, `config/`, `data/`, `deploy/`, `docs/`, `lib/`, `logs/`, `memory/`, `scripts/`.
- [ ] `.gitignore` covers `.env`, `data/`, `logs/`, `__pycache__/`, `.venv/`.
- [ ] `lib/trading_platform` added as a git submodule. `.gitmodules` committed. Pinned to a specific commit — updates via `git submodule update --remote` are deliberate.
- [ ] `.env.example` committed with all keys blanked.
- [ ] `pyproject.toml` runtime deps: `alpaca-py`, `anthropic`, `pandas`, `pandas-ta`, `pandas_market_calendars`, `structlog`, `python-dotenv`, `fastapi`, `uvicorn`, `jinja2`. No `redis`.
- [ ] `pyproject.toml` dev extras: `pytest`, `pytest-asyncio`, `httpx`, `ruff`.
- [ ] `deploy/Dockerfile`: python:3.11-slim, non-root user, `pip install -e /app/lib/trading_platform` so `from persistence.db import …` works at runtime (not just in pytest).
- [ ] `deploy/docker-compose.yml`: services `agent` and `dashboard`, both with `env_file: .env`, both mounting `./data` and `./logs`.
- [ ] `tests/conftest.py` prepends `src/`, `lib/trading_platform/src/`, and `scripts/` to `sys.path`.
- [ ] `src/_bootstrap.py` does the equivalent sys.path setup for scripts that run outside the installed package (used by `scripts/*.py`).
- [ ] `src/schema.sql` contains every table from `ARCHITECTURE §3.6` (including `risk_state`, `llm_calls`, `latency_traces`). One-shot create; Phase 0 creates all tables even though most aren't written to until later phases.
- [ ] Stub `src/dashboard/app.py` with a single `/health` → `{"ok": true}` route so the Phase 0 exit criterion is valid.
- [ ] `scripts/hello.py` — smoke pings Alpaca `get_account()` and Anthropic.

**Exit criteria**
- [ ] `docker compose up` brings up both services with no errors.
- [ ] `curl http://localhost:8000/health` returns `{"ok": true}`.
- [ ] `python scripts/hello.py` prints paper account equity and an Anthropic 10-token response.
- [ ] `python -m pytest tests/ -q` passes (smoke tests only).
- [ ] `python -c "from persistence.db import init_db; init_db('data/events.db', open('src/schema.sql').read())"` runs inside the agent container and creates every table listed in `ARCHITECTURE §3.6`.

---

## Phase 1 — Data agent (Days 2–4, ~6 hours)

**Deliverables**
- [ ] `src/market/alpaca_ws.py` subscribes to Alpaca's stock data websocket for a 10-symbol watchlist (SPY, QQQ, AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AMD).
- [ ] `src/market/alpaca_rest.py` fetches 60-day daily bars on startup; writes to `bars_daily`.
- [ ] `src/market/indicators.py` computes RSI-14, SMA-20/50/200, 20d avg volume, ATR-14 via `pandas-ta`.
- [ ] `src/market/data_agent.py` writes each snapshot as a `snapshots` row: `(symbol, ts, price, rsi14, sma20, sma50, sma200, avg_vol_20, atr14, price_vs_sma50_pct)`. **No `stale` column** — freshness is a read-time filter, not state.
- [ ] In-process latest-snapshot cache uses `trading_platform.data.TTLCache` (90s TTL); writes are always write-through to SQLite.
- [ ] `LatencyTracker` wired around websocket receive, indicator compute, DB write; marks land in `logs/latency.jsonl`.
- [ ] Retention job prunes `snapshots` older than 2 trading days per tick.

**Exit criteria**
- [ ] `sqlite3 data/events.db "SELECT symbol, MAX(ts) FROM snapshots GROUP BY symbol"` shows all 10 symbols within 60s of wall clock during market hours.
- [ ] `tests/test_indicators.py`: synthetic OHLCV → asserted RSI within 0.1.
- [ ] `tests/test_snapshot_repo.py`: write → read round-trip, plus a 90s-threshold filter test.
- [ ] `logs/latency.jsonl` has per-cycle records with `data_fetch_ms` and `indicators_ms` marks.

---

## Phase 2 — Strategy agent, rule-only (Days 5–7, ~6 hours)

**Deliverables**
- [ ] `src/strategies/mean_reversion.py` — emits `Signal(symbol, "buy", …)` when `rsi14 < 30 AND volume_today > 1.5 * avg_vol_20 AND price > sma200`.
- [ ] `src/strategies/momentum.py` — emits buy when `price > sma50 > sma200 AND 50 <= rsi14 <= 70 AND close > yesterday_close`.
- [ ] `src/strategies/__init__.py` loads enabled strategies from `config/strategies.yaml`.
- [ ] `src/strategies/agent.py` reads latest non-stale snapshots (`ts > now - 90`), fans out to each strategy, merges signals (most recent per symbol).
- [ ] Signals written to `signals` with FK `tick_id`.
- [ ] `scripts/replay.py` — reads `bars_daily` for a given date, synthesizes per-minute ticks, runs both strategies, prints signals to stdout. **This is the dry-run harness.**

**Exit criteria**
- [ ] `python scripts/replay.py --date YYYY-MM-DD` produces signals that pass eyeball review.
- [ ] `tests/test_strategies.py`: malformed/partial snapshot → no signal, no exception.

---

## Phase 3 — Risk agent + kill switch (Days 8–10, ~5 hours)

**Deliverables**
- [ ] `src/risk_rules/rules.py` — each rule from `ARCHITECTURE §3.3` as a `trading_platform.risk.RiskRule` returning a `RiskVerdict`.
- [ ] `src/risk_rules/agent.py` composes rules into a `trading_platform.risk.RuleBasedPolicy`.
- [ ] `src/risk_rules/sizing.py` — `qty = floor(min(equity * 0.03, equity * 0.50 - current_exposure) / price)`. **Decimal only** (per CLAUDE.md).
- [ ] `src/risk_rules/portfolio.py` fetches current positions + equity from Alpaca on every call.
- [ ] Earnings blackout — Polygon endpoint, 2-day window.
- [ ] Kill switch: if `/app/data/KILL` exists, reject all signals and, if positions exist, emit a single `FLATTEN_ALL`.
- [ ] Daily halt via `trading_platform.risk.CircuitBreaker`: at −2% of starting equity, write `risk_state(trading_date, halted=1, engaged_at, reason)`. On startup, read today's row and resume.

**Exit criteria**
- [ ] `tests/test_risk_rules.py` — one test per rule: (signal, portfolio) → approve/reject + reason.
- [ ] Manual: `touch data/KILL` → next tick, all signals rejected and logged.
- [ ] Manual: seed `risk_state` to simulate a −2.5% day → halt persists across `docker compose restart agent` and clears only on date rollover.

---

## Phase 4 — Execution agent (Days 11–12, ~4 hours)

**Deliverables**
- [ ] `src/execution/agent.py` takes approved signals → bracket orders via `alpaca-py`.
- [ ] Default stop −2%, TP +4%; both overridable per strategy in `config/strategies.yaml`.
- [ ] Idempotency: `client_order_id = {tick_id}:{symbol}`; broker-side dedup.
- [ ] **Polling-based fill tracking** (not webhooks — paper endpoint has no public ingress): poll `/v2/orders` every tick and upsert into `fills`.
- [ ] Broker rejection → log full response body, fire alert, no automatic retry.

**Exit criteria**
- [ ] Manual paper: tiny signal placed end-to-end; bracket visible in Alpaca UI with both stop and TP attached (no naked entries).
- [ ] `tests/test_execution.py` covers: idempotent client_order_id, rejection path logs response, no silent retries.

---

## Phase 5 — Orchestrator + logging + LLM judgment (Days 13–16, ~12 hours)

**Deliverables**
- [ ] `src/orchestrator/main.py` asyncio loop.
- [ ] `src/orchestrator/calendar.py` uses `pandas_market_calendars` to gate ticks on real NYSE session (holidays, half-days, early close).
- [ ] Tick cadence: 5 min within 09:45 → close-minus-15min ET; idle otherwise.
- [ ] Per-tick flow: `LatencyTracker.start_cycle()` → snapshots → strategies → (optional) LLM judgment → risk → execute → log, with `.mark()` at every boundary.
- [ ] `src/llm/judge.py` — per rule-signal, call Claude Haiku 4.5 with prompt-cached system + schema block, plus the snapshot + up to 3 Polygon headlines. Expected output: JSON `{"approve": bool, "reason": str, "confidence": 0-1}`.
- [ ] `src/llm/cost_tracker.py` — sums tokens × price per trading day; hard cap `MAX_LLM_DAILY_USD=5`; above it, judgment is skipped (rule-only).
- [ ] Every LLM call writes to `llm_calls`: prompt hash, response, tokens_in/out, cost_usd, cache_hit, latency_ms, model.
- [ ] `src/orchestrator/logging.py` configures structlog → `logs/YYYY-MM-DD.jsonl`; a custom redactor processor strips API keys before write.
- [ ] `src/orchestrator/daily_summary.py` cron at 16:30 ET → `logs/daily_summary_YYYY-MM-DD.md` (P&L, trades, LLM cost, top signals, p50/p95 per-stage latency).
- [ ] `trading_platform.alerting.AlertDispatcher` wired for: fills, broker rejections, daily halt engagement, kill-switch activation. Backend configured in `.env` (Gmail or Telegram).
- [ ] A/B shadow ledger: `signals.llm_branch` column recording `rule_only | llm_approved | llm_rejected` so executed trades can be attributed in Phase 6. No counterfactual modeling — we compare executed trades only.

**Exit criteria**
- [ ] One full market day runs end-to-end with zero unhandled exceptions.
- [ ] Daily summary is written on that day.
- [ ] `logs/latency.jsonl` produced every tick; `python -c "from observability import analyze_latency; analyze_latency('logs/latency.jsonl')"` prints per-stage p50/p95.
- [ ] LLM cost cap triggers correctly (unit test: seed cost > cap → judgment skipped).
- [ ] A calendar holiday in the window (or a mocked one in test) results in zero ticks.

---

## Phase 5b — Dashboard (Days 17–19, ~6 hours)

**Deliverables**
- [ ] `src/dashboard/` FastAPI app, SQLite opened read-only (`sqlite:///…?mode=ro`), `logs/latency.jsonl` read on demand.
- [ ] Views per `ARCHITECTURE §3.7`: Today, Signals, Latency, Daily summary, Admin.
- [ ] Styling per `DESIGN.md`: Aeonik Pro display (weight 500, tight tracking), Inter body (positive letter-spacing), pill buttons (9999px radius), zero shadows, `#191c1f` + white base, `--rui-*` semantic tokens (teal `#00a87e` positive, danger `#e23b4a` negative).
- [ ] Dashboard service in `deploy/docker-compose.yml` bound to `127.0.0.1:8000`.
- [ ] Admin view: single confirm-to-engage kill-switch button that creates `data/KILL`. This is the dashboard's **only** write path.

**Exit criteria**
- [ ] `tests/test_dashboard.py`: each view renders; DB connection is read-only (a write attempt raises); kill-switch endpoint refuses non-localhost origins.
- [ ] Manual: Today view equity curve matches Alpaca's paper UI; Latency view matches `analyze_latency` output.
- [ ] Killing the dashboard process does not affect the agent; dashboard has no broker credentials in its env.

---

## Phase 6 — Paper soak (Weeks 3–6, passive)

**Deliverables**
- [ ] VPS deployment (Hetzner CX22 or DO basic), `docker compose up -d`.
- [ ] 3 consecutive market days of clean operation *before* the 4-week soak clock starts (this is the stability gate Phase 5 didn't own).
- [ ] 4 full trading weeks (20 trading days) of uninterrupted paper operation.
- [ ] `docs/CHANGELOG.md` started on day 1 of soak. Every change = one entry (date, rationale, A/B observation). **Max one change per week.**
- [ ] Weekly review ritual every Sunday: read last week's daily summaries, log anomalies in CHANGELOG.

**Exit criteria — all must hold**
- [ ] Sharpe on paper ≥ 0.5 over soak period (sanity floor, not a goal).
- [ ] Max drawdown < 5%.
- [ ] Zero Risk Agent bypasses or overrides (grep logs).
- [ ] Zero unhandled exceptions in the orchestrator over the last 10 trading days.
- [ ] On actually-executed trades, LLM-approved P&L ≥ rule-only P&L over the soak. If not, disable the LLM stage (set `llm.enabled: false` in config) and re-baseline. **No counterfactual modeling required** — we judge only trades that actually filled.
- [ ] Operator can answer "why did I take the AAPL trade on day 12?" via dashboard (Signals view → trace link) or SQLite chain from `ARCHITECTURE §3.6`.
- [ ] Tick p95 latency stable across soak; no stage > 3s for ≥99% of ticks.

---

## Phase 7 — Iterate (ongoing)

Only after Phase 6 passes. Priority order:
1. [ ] Expand watchlist (10 → 20, then 20 → 50).
2. [ ] Add sector rotation as a third strategy.
3. [ ] Portfolio-level regime filter (e.g., no new longs if SPY < SMA-200).
4. [ ] Defined-risk options only (verticals, covered calls). New `src/strategies/options_*.py`. **No naked options.**
5. [ ] Revisit LLM: if the judgment stage earned its keep, consider Sonnet 4.6 on the final decision only.

CHANGELOG discipline continues: one entry per change, with a paper A/B result where possible.

---

## Phase 8 — Live readiness checklist (gated, do not skim)

Before flipping to live, **all** boxes must be checked:

- [ ] Phase 6 exit criteria held for ≥4 weeks.
- [ ] Kill-switch drill executed in paper; flatten-all confirmed within one tick.
- [ ] Survived ≥1 volatile day (>2% SPY move) in paper; logs reviewed.
- [ ] Operator understands every trade from last week. Not "mostly." Every one.
- [ ] Real-money loss cap set at a level the operator is psychologically fine losing completely (1–5% of paper-equivalent balance to start).
- [ ] Live endpoint gated on `LIVE=1` env AND interactive "type YES to trade real money" prompt at startup.
- [ ] Paper instance continues in parallel as a control.
- [ ] Verified (not newly built — wired in Phase 5): alerts fire on fills, rejections, daily halt, kill-switch activation.
- [ ] Talked to a CPA about trader tax status / wash sales, or at minimum read the relevant IRS pub.

If any box is unchecked, you are not ready.

---

## Budget (paper phase, first 2 months)

| Item | Cost |
|---|---|
| Alpaca paper | $0 |
| Anthropic (Haiku, ~1500 calls/day, cached) | ~$10–25/mo |
| Polygon.io (free tier → $29/mo when news matters) | $0 → $29 |
| VPS (Hetzner CX22 or DO basic) | ~$5/mo |
| **Total** | **~$15–60/mo** during paper |

## Timeline at a glance

- **Week 1**: Phases 0–2 (data + rules, no orders)
- **Week 2**: Phases 3–5 (risk, execution, orchestrator, LLM)
- **Week 3 start**: Phase 5b (dashboard) — ~6h before the soak begins, so review has a UI from day 1
- **Weeks 3–6**: Phase 6 (paper soak, hands off, weekly review)
- **Week 7+**: Phase 7 iteration; eventually Phase 8 gate

Total to "is this working": ~6 weeks calendar, ~40–50 hours of hands-on work.
