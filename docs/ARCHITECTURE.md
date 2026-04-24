# AI Trading Agent — Architecture

## 1. Goals & Non-Goals

### Goals
- **Paper-trade first.** Prove a strategy works on Alpaca paper for ≥4 weeks before any real capital.
- **Auditable decisions.** Every order traceable to the inputs, rule, and LLM call that produced it.
- **Hard risk limits enforced in code**, not in prompts. The model cannot override position caps, daily loss caps, or the kill switch.
- **Cheap to run.** Target <$50/month for compute + LLM during paper phase.
- **Single operator.** One person (the user) runs this. No multi-tenant, no SSO, no team.

### Non-Goals (explicit)
- Not HFT. Decision cadence is minutes, not microseconds.
- Not market-making, arbitrage, or latency-sensitive strategies.
- Not a framework. No plugin system, no abstraction for "swap the broker." One broker (Alpaca), one data provider to start.
- Not beating the S&P. Realistic goal is *learning the craft without blowing up* and, eventually, risk-adjusted returns comparable to buy-and-hold.

## 2. System Overview

```
                    ┌─────────────────────────────────────────┐
                    │              Orchestrator                │
                    │    (asyncio loop, 1 tick per 5 min)      │
                    └────────────┬────────────────────────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
        ▼                        ▼                        ▼
 ┌─────────────┐         ┌──────────────┐         ┌─────────────┐
 │ Data Agent  │  ───►   │ Strategy     │  ───►   │ Risk Agent  │
 │             │         │ Agent        │         │ (gatekeeper)│
 │ Alpaca WS   │         │              │         │             │
 │ Polygon     │         │ rules.py +   │         │ hard caps   │
 │ snapshots   │         │ Claude Haiku │         │ kill switch │
 └─────┬───────┘         └──────┬───────┘         └─────┬───────┘
       │                        │                       │
       │                        │                       ▼ (approved only)
       │                        │                ┌────────────────┐
       │                        │                │Execution Agent │
       │                        │                │ alpaca-py      │
       │                        │                │ paper endpoint │
       │                        │                └───────┬────────┘
       │                        │                        │
       ▼                        ▼                        ▼
 ┌────────────────────────────────────────────────────────────────┐
 │  SQLite (data/events.db)  +  logs/YYYY-MM-DD.jsonl  +          │
 │  logs/latency.jsonl        (single source of truth)            │
 └──────────────────────────────┬─────────────────────────────────┘
                                │ (read-only)
                                ▼
                    ┌───────────────────────┐
                    │  Dashboard (FastAPI)  │
                    │  /positions /pnl      │
                    │  /signals /latency    │
                    │  Revolut-styled (DESIGN.md) │
                    └───────────────────────┘
```

All persistent state lives in a single SQLite file. There is no Redis. A short-lived in-process dict holds the latest snapshot for the current tick only; after use it is written through to SQLite so the dashboard and any restart can rebuild state from disk.

## 3. Components

### 3.1 Data Agent (`src/data_agent.py`)
- Subscribes to Alpaca market data websocket for a configured watchlist (initially 10–20 liquid large-caps).
- Pulls daily bars + 20-day history from Polygon REST on startup.
- Writes latest quote + indicators (RSI-14, 20d volume avg, 50/200 SMA) to the `snapshots` table in SQLite, keyed by `(symbol, ts)`. The Strategy Agent reads the latest row per symbol; rows older than 90s → `stale=1` and not eligible for trading.
- A retention job prunes `snapshots` to the last 2 trading days on each tick; historical context beyond that lives in `bars_daily`.
- **No decisions here.** Pure I/O + feature computation.

### 3.2 Strategy Agent (`src/strategy_agent.py`)
Two-stage pipeline:

1. **Rule filter (Python, deterministic):** iterate watchlist, apply rules from `strategies/*.py`. Example: `rsi_14 < 30 AND volume_today > 1.5 * avg_volume_20d`. Rules return `Signal(symbol, side, confidence, reason)`.
2. **LLM judgment (Claude Haiku 4.5, optional):** for each signal from step 1, call Claude with the quote data + recent news headlines (from Polygon news endpoint) and ask for a YES/NO + one-sentence reason. Prompt is cached (system + indicators schema).

Output: `List[Signal]` handed to Risk Agent. **Strategy never sends orders.**

### 3.3 Risk Agent (`src/risk_agent.py`) — the gatekeeper
Hard rules, all enforced in plain Python (no LLM involvement):

| Rule | Default |
|---|---|
| Max position size per symbol | 3% of equity |
| Max total exposure | 50% of equity (paper phase) |
| Max daily loss | 2% of equity → auto-flatten + halt |
| Max open positions | 8 |
| Trading hours | 09:45 – 15:45 ET (avoid open/close volatility) |
| Blackout: earnings | No new positions 2 trading days before earnings |
| Kill switch | File `/app/KILL` present → no orders, flatten all |

Risk Agent returns `Approved(signal, sized_qty)` or `Rejected(signal, reason)`. Both are logged.

Risk state that must survive a process restart (daily halt flag, kill-switch engaged history, cumulative P&L for the day) is written to the SQLite `risk_state` table, keyed by trading date.

### 3.4 Execution Agent (`src/execution_agent.py`)
- Receives only approved signals.
- Places bracket orders via `alpaca-py` (entry + stop-loss + take-profit).
- Default stop: −2% from entry. Default take-profit: +4%. Overridable per strategy.
- Always uses paper endpoint in Phase 0–5. Live endpoint requires explicit config flag + human confirmation on each session start.

### 3.5 Orchestrator (`src/main.py`)
- Asyncio loop. One tick = data refresh → strategy → risk → execution.
- Tick cadence: 5 min during market hours, idle otherwise.
- Handles graceful shutdown (SIGTERM → flatten? no — just stop sending new orders; existing brackets remain).
- Wraps each tick in `trading_platform.observability.LatencyTracker.start_cycle()` and calls `.mark()` at every stage boundary (`data_fetched`, `features_computed`, `signals_generated`, `llm_judged`, `risk_decided`, `orders_sent`).

### 3.6 Event Log + Latency Trace
All persisted events live in SQLite at `data/events.db`, initialized via `trading_platform.persistence.db.init_db()` (SQLite dialect; the same schema works against Postgres if we ever relocate).

Core tables:

| Table | Purpose |
|---|---|
| `ticks` | one row per orchestrator tick (id, started_at, finished_at, status) |
| `snapshots` | per-symbol quote + indicators per tick |
| `signals` | rule-generated signals, FK `tick_id` |
| `llm_calls` | per-judgment call: prompt, response, tokens_in/out, cost_usd, cache_hit, latency_ms, model |
| `risk_decisions` | approve/reject + reason, FK `signal_id` |
| `orders` | broker submissions, FK `risk_decision_id` |
| `fills` | partial/full fills keyed to `orders.client_order_id` |
| `risk_state` | persistent halt flags, daily P&L checkpoints, kill-switch engagements |
| `latency_traces` | per-stage elapsed ms per tick (aggregated view of the JSONL stream) |

Parallel streams for grep-ability:
- `logs/YYYY-MM-DD.jsonl` — structured event log via `structlog`.
- `logs/latency.jsonl` — `LatencyTracker` output (one JSON object per cycle + per candidate), analyzable with `trading_platform.observability.analyze_latency`.

Traceability invariant (from CLAUDE.md): `fills.order_id → orders.risk_decision_id → risk_decisions.signal_id → signals.tick_id → ticks.id`, with `signals.llm_call_id` present when the judgment stage ran. No new decision surface ships without writing through both SQLite and the JSONL stream.

### 3.7 Dashboard (`src/dashboard/`)
A local FastAPI + Jinja app that reads `data/events.db` and `logs/latency.jsonl` read-only. Styled per `DESIGN.md` (Revolut-inspired: Aeonik Pro display, Inter body, pill buttons, zero shadows, near-black + white marketing chrome with `--rui-*` semantic tokens for P&L/state color).

Views (in priority order):
1. **Today** — equity curve, open positions, today's orders, halt status, kill-switch status.
2. **Signals** — rule-generated vs LLM-approved, filterable by symbol/strategy; each row links to the full trace (tick → signal → risk decision → order → fill).
3. **Latency** — p50/p95/max per stage, rendered from `latency_traces` and the JSONL stream. The "is the LLM worth it?" question from Phase 6 lives here.
4. **Daily summary** — rendered `logs/daily_summary_YYYY-MM-DD.md` per day.
5. **Admin** — read-only view of `risk_state`; a *single* button to flip the kill switch (writes `data/KILL`; authenticated via localhost-only binding in paper phase).

The dashboard never writes to agent state beyond the kill-switch file. It is a separate process in its own container; it cannot place orders, clear halt flags, or mutate `events.db`.

## 4. Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11 | `alpaca-py`, `pandas-ta`, `anthropic` all first-class |
| Broker | Alpaca (paper → live) | Free paper, clean API, free market data tier |
| Market data | Alpaca (bars/quotes) + Polygon.io ($29/mo tier) for news & historical | Alpaca alone is fine to start; add Polygon when news matters |
| LLM | Claude Haiku 4.5 default, Sonnet 4.6 for judgment calls | Haiku is ~$10/mo at this cadence; Sonnet only when the rule is ambiguous |
| Persistence | SQLite (WAL mode) via `trading_platform.persistence.db` | Single file, single writer, trivial backup, plenty fast at this scale; Postgres dialect available without code changes if we outgrow it |
| Shared library | `trading_platform` (git submodule at `lib/trading_platform`) | Provides `BasePipeline`, `PriorityQueue`, `RuleBasedPolicy`, `CircuitBreaker`, `MetricsCollector`, `LatencyTracker`, `AlertDispatcher`, `TTLCache`, and env helpers. We import it; we do not fork it. |
| Dashboard | FastAPI + Jinja (server-rendered), styled per `DESIGN.md` | Read-only window onto SQLite + `latency.jsonl`; no SPA build toolchain needed |
| Container | Docker + docker-compose | `docker compose up` starts agent + dashboard |
| Host (paper phase) | Local Mac or $5/mo VPS | No GPU, no colo needed |
| Secrets | `.env` file, `python-dotenv`, never committed | Alpaca keys, Anthropic key, Polygon key |
| Observability | `structlog` → JSONL + SQLite + optional Grafana Cloud free tier | Can add later |

## 5. Deployment Topology

### Phase: paper (local)
```
[your Mac] → docker compose up
    ├── agent container       (Python app — orchestrator + all agents)
    ├── dashboard container   (FastAPI, port 8000, localhost-bound)
    └── volumes:
         ./data   → SQLite events.db + KILL switch file
         ./logs   → JSONL event + latency streams, daily summaries
```

### Phase: paper (always-on)
Same compose file on a $5–10/mo VPS (Hetzner, DigitalOcean). Add `watchtower` for auto-pull of new images, or just `git pull && docker compose up -d --build` on deploy.

### Phase: live (future, gated)
- Move to AWS us-east-1 (Alpaca's region) for lower latency.
- Enable live endpoint flag.
- Paper continues in parallel on same VPS (separate compose project) as a control.

## 6. Security

- `.env` is `.gitignore`'d. Keys never printed to logs (redactor in `structlog`).
- Container runs as non-root user.
- Kill switch is a local file, not an API — no remote can flip it.
- Live trading requires two things: env flag `LIVE=1` AND a manual prompt at startup. No auto-enable.

## 7. Failure Modes & Responses

| Failure | Response |
|---|---|
| Alpaca websocket disconnects | Reconnect with backoff; skip ticks during outage; do not trade on stale data (>90s old → abstain) |
| Polygon/news API down | Strategy proceeds without news; LLM judgment stage skipped; log degraded mode |
| Anthropic API down | Skip LLM judgment stage; rule-only signals still flow; log degraded mode |
| Broker rejects order | Log + alert; do not retry blindly (could be margin/PDT/halt) |
| Data inconsistency (e.g., negative price) | Reject signal in Risk Agent; log anomaly |
| Daily loss limit hit | Flatten all, halt orchestrator, require manual restart next day |
| Orchestrator crash | Docker restart policy = `on-failure`; bracket orders protect open positions while down |

## 8. Observability

Per tick, log:
- tick_id, timestamp, watchlist symbols processed
- features computed (count, any NaN)
- signals generated (count, detail)
- risk decisions (approved/rejected, reason)
- orders placed (id, symbol, qty, type)
- LLM calls (model, tokens_in, tokens_out, cost_usd, cache_hit)

Daily summary job: equity curve, # trades, win rate, avg win/loss, total LLM cost, time in market. Written to `logs/daily_summary_YYYY-MM-DD.md`.

## 9. Directory Layout

The repo follows this shape (see screenshot reference):

```
.claude/              Claude Code project config
.github/              CI workflows (tests on PR)
.vscode/              optional editor config
config/               YAML/JSON configs (watchlists, risk params, strategy toggles, LLM budget)
data/                 SQLite events.db, KILL switch file — gitignored
deploy/               Dockerfile, docker-compose.yml, VPS provisioning
docs/                 ARCHITECTURE.md, EXECUTION_PLAN.md, CHANGELOG.md
lib/trading_platform/ git submodule — shared platform library
logs/                 JSONL event + latency streams, daily_summary_*.md — gitignored
memory/               claude_session-style durable notes (decisions.md, current.md)
scripts/              one-offs: replay, backfill, kill-switch drill
src/                  agent + dashboard source (see §3, imports with PYTHONPATH=src)
tests/                pytest suite; conftest.py wires src/ and lib/trading_platform/src/ onto sys.path
.env / .env.example   secrets (gitignored) and template
.gitmodules           pins lib/trading_platform
AGENTS.md             short ops notes for agents contributing to the repo
CLAUDE.md             guidance for Claude Code sessions (invariants, commands)
DESIGN.md             dashboard design system (Revolut-inspired)
```

## 10. Shared Platform Dependency (`lib/trading_platform`)

The repo consumes `trading_platform` as a git submodule under `lib/`. Import style is flat (matches the platform's own layout):

```python
from pipeline import BasePipeline, PriorityQueue
from risk import RuleBasedPolicy, CircuitBreaker
from observability import MetricsCollector, LatencyTracker
from persistence.db import init_db, get_db
from alerting import AlertDispatcher
from contracts import SubmissionRef, VerificationOutcome, RiskVerdict
```

What we use it for:
- **`persistence.db.init_db`**: the single code path that opens `data/events.db` with WAL + PRAGMA tuning.
- **`observability.LatencyTracker`**: per-tick + per-candidate latency records to `logs/latency.jsonl`.
- **`observability.MetricsCollector`**: counters (signals, approvals, rejections, orders, LLM cache hits).
- **`risk.RuleBasedPolicy` / `CircuitBreaker`**: the Risk Agent is a `RuleBasedPolicy`; the daily-loss halt is a `CircuitBreaker`.
- **`pipeline.BasePipeline`**: Strategy → (optional) LLM judgment → Risk → Execution runs as one pipeline per signal.
- **`alerting.AlertDispatcher`**: Gmail/Discord/Telegram backends for fills, rejections, halts, kill-switch events.
- **`data.TTLCache`**: in-process snapshot cache for the current tick (the write-through path lands in SQLite).

What we do **not** do:
- Fork or modify `trading_platform`. If something is missing, open a PR upstream.
- Add Alpaca- or equity-specific types to the platform. The platform is broker-agnostic; adapters live in `src/`.

## 11. Open Questions to Resolve Before Live

1. Which strategies actually have edge in paper over 4+ weeks?
2. What's the LLM's marginal contribution vs. rule-only baseline? (Run both in parallel and measure.)
3. Tax lot handling — FIFO is default; is that acceptable for the user's tax situation?
4. PDT (Pattern Day Trader) rules — do we cap day trades to stay under the 4-in-5-days threshold on sub-$25k accounts?
5. Do we need options at all in v1? (Recommendation: no. Stocks only until the core loop is boring.)
