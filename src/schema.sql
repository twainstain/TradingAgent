-- Event store for the trading agent. Initialized via trading_platform.persistence.db.init_db().
-- One-shot create. Most tables only start receiving writes in later phases, but all are created
-- up front so Phase 0 can verify the schema is complete.
--
-- Traceability invariant (CLAUDE.md / ARCHITECTURE §3.6):
--   fills.order_id -> orders.risk_decision_id -> risk_decisions.signal_id -> signals.tick_id -> ticks.id
--   with signals.llm_call_id present when the judgment stage ran.

PRAGMA foreign_keys = ON;

-- One row per orchestrator tick.
CREATE TABLE IF NOT EXISTS ticks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT,
    status       TEXT    NOT NULL DEFAULT 'running'    -- running | ok | error | halted | killed
);
CREATE INDEX IF NOT EXISTS ix_ticks_started_at ON ticks(started_at);

-- Daily bars (60-day backfill on startup; historical context beyond `snapshots` retention).
CREATE TABLE IF NOT EXISTS bars_daily (
    symbol       TEXT    NOT NULL,
    date         TEXT    NOT NULL,          -- ISO date
    open         REAL    NOT NULL,
    high         REAL    NOT NULL,
    low          REAL    NOT NULL,
    close        REAL    NOT NULL,
    volume       INTEGER NOT NULL,
    PRIMARY KEY (symbol, date)
);

-- Per-symbol quote + indicators per tick. Freshness is a read-time filter (>90s -> ineligible),
-- never a stored state column.
CREATE TABLE IF NOT EXISTS snapshots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id                 INTEGER REFERENCES ticks(id) ON DELETE SET NULL,
    symbol                  TEXT    NOT NULL,
    ts                      TEXT    NOT NULL,
    price                   REAL    NOT NULL,
    rsi14                   REAL,
    sma20                   REAL,
    sma50                   REAL,
    sma200                  REAL,
    avg_vol_20              REAL,
    atr14                   REAL,
    price_vs_sma50_pct      REAL
);
CREATE INDEX IF NOT EXISTS ix_snapshots_symbol_ts ON snapshots(symbol, ts DESC);
CREATE INDEX IF NOT EXISTS ix_snapshots_tick      ON snapshots(tick_id);

-- Rule-generated signals (may or may not be gated by LLM judgment).
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id      INTEGER NOT NULL REFERENCES ticks(id) ON DELETE CASCADE,
    symbol       TEXT    NOT NULL,
    strategy     TEXT    NOT NULL,
    side         TEXT    NOT NULL,            -- buy | sell
    confidence   REAL,
    reason       TEXT,
    llm_call_id  INTEGER REFERENCES llm_calls(id) ON DELETE SET NULL,
    llm_branch   TEXT,                         -- rule_only | llm_approved | llm_rejected (Phase 5)
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_signals_tick   ON signals(tick_id);
CREATE INDEX IF NOT EXISTS ix_signals_symbol ON signals(symbol);

-- One row per Claude call. Prompt + response stored for full audit.
CREATE TABLE IF NOT EXISTS llm_calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    model        TEXT    NOT NULL,
    prompt_hash  TEXT    NOT NULL,
    prompt       TEXT    NOT NULL,
    response     TEXT,
    tokens_in    INTEGER,
    tokens_out   INTEGER,
    cost_usd     REAL,
    cache_hit    INTEGER NOT NULL DEFAULT 0,   -- 0/1
    latency_ms   INTEGER,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_llm_calls_created ON llm_calls(created_at);

-- Risk Agent verdict. Approvals and rejections are BOTH persisted.
CREATE TABLE IF NOT EXISTS risk_decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id    INTEGER NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
    approved     INTEGER NOT NULL,             -- 0/1
    reason       TEXT,
    sized_qty    INTEGER,                      -- NULL on reject
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_signal ON risk_decisions(signal_id);

-- Broker submissions. client_order_id = "{tick_id}:{symbol}" (idempotency key).
CREATE TABLE IF NOT EXISTS orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    risk_decision_id    INTEGER NOT NULL REFERENCES risk_decisions(id) ON DELETE CASCADE,
    client_order_id     TEXT    NOT NULL UNIQUE,
    broker_order_id     TEXT,
    symbol              TEXT    NOT NULL,
    side                TEXT    NOT NULL,
    qty                 INTEGER NOT NULL,
    type                TEXT    NOT NULL,       -- bracket | market | limit
    entry_price         REAL,
    stop_price          REAL,
    take_profit_price   REAL,
    status              TEXT    NOT NULL,
    submitted_at        TEXT    NOT NULL,
    raw_response        TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_broker_id      ON orders(broker_order_id);
CREATE INDEX IF NOT EXISTS ix_orders_risk_decision  ON orders(risk_decision_id);

-- Fills polled from /v2/orders. Keyed to orders.client_order_id.
CREATE TABLE IF NOT EXISTS fills (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    client_order_id   TEXT    NOT NULL,
    filled_qty        INTEGER NOT NULL,
    filled_avg_price  REAL,
    status            TEXT    NOT NULL,
    reported_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fills_client_order ON fills(client_order_id);

-- Sticky per-day halt + kill-switch history. Survives process restart; cleared only on date rollover.
CREATE TABLE IF NOT EXISTS risk_state (
    trading_date          TEXT    PRIMARY KEY,  -- YYYY-MM-DD ET
    halted                INTEGER NOT NULL DEFAULT 0,
    engaged_at            TEXT,
    reason                TEXT,
    starting_equity       REAL,
    current_pnl           REAL,
    kill_switch_engaged   INTEGER NOT NULL DEFAULT 0
);

-- Per-stage latency marks aggregated from logs/latency.jsonl.
CREATE TABLE IF NOT EXISTS latency_traces (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id       INTEGER REFERENCES ticks(id) ON DELETE CASCADE,
    stage         TEXT    NOT NULL,            -- data_fetched | features_computed | signals_generated | llm_judged | risk_decided | orders_sent
    elapsed_ms    REAL    NOT NULL,
    recorded_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_latency_tick  ON latency_traces(tick_id);
CREATE INDEX IF NOT EXISTS ix_latency_stage ON latency_traces(stage);
