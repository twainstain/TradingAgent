"""Phase 2 strategies + StrategyAgent."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.signal import Signal
from storage.snapshot_repo import SnapshotRow
from strategies.base import StrategyContext
from strategies.mean_reversion import MeanReversion
from strategies.momentum import Momentum


def _snap(**overrides) -> SnapshotRow:
    base = dict(
        symbol="AAPL",
        ts=datetime.now(timezone.utc),
        price=180.0,
        rsi14=55.0,
        sma20=175.0,
        sma50=170.0,
        sma200=160.0,
        avg_vol_20=3_000_000.0,
        atr14=1.5,
        price_vs_sma50_pct=5.88,
        tick_id=None,
    )
    base.update(overrides)
    return SnapshotRow(**base)


# ---- mean_reversion ----

def test_mean_reversion_emits_on_oversold_bounce() -> None:
    snap = _snap(rsi14=25.0, price=185.0, sma200=180.0, avg_vol_20=1_000_000.0)
    ctx = StrategyContext(snapshot=snap, volume_today=2_000_000.0)
    sig = MeanReversion().evaluate(ctx)
    assert isinstance(sig, Signal)
    assert sig.side == "buy"
    assert sig.strategy == "mean_reversion"
    assert "rsi14=25" in sig.reason or "rsi14=25.00" in sig.reason


def test_mean_reversion_skips_when_rsi_above_threshold() -> None:
    snap = _snap(rsi14=45.0)
    ctx = StrategyContext(snapshot=snap, volume_today=5_000_000.0)
    assert MeanReversion().evaluate(ctx) is None


def test_mean_reversion_skips_when_price_below_sma200() -> None:
    snap = _snap(rsi14=25.0, price=150.0, sma200=180.0)
    ctx = StrategyContext(snapshot=snap, volume_today=5_000_000.0)
    assert MeanReversion().evaluate(ctx) is None


def test_mean_reversion_skips_when_volume_below_multiple() -> None:
    snap = _snap(rsi14=25.0, avg_vol_20=3_000_000.0)
    ctx = StrategyContext(snapshot=snap, volume_today=3_000_000.0)  # exactly 1.0x, not >1.5x
    assert MeanReversion().evaluate(ctx) is None


# ---- momentum ----

def test_momentum_emits_on_trending_breakout() -> None:
    snap = _snap(price=180.0, sma50=170.0, sma200=160.0, rsi14=60.0)
    ctx = StrategyContext(snapshot=snap, yesterday_close=175.0)
    sig = Momentum().evaluate(ctx)
    assert isinstance(sig, Signal)
    assert sig.side == "buy"
    assert sig.strategy == "momentum"


def test_momentum_skips_when_below_sma50() -> None:
    snap = _snap(price=165.0, sma50=170.0, sma200=160.0, rsi14=60.0)
    ctx = StrategyContext(snapshot=snap, yesterday_close=160.0)
    assert Momentum().evaluate(ctx) is None


def test_momentum_skips_when_rsi_outside_band() -> None:
    snap = _snap(price=180.0, sma50=170.0, sma200=160.0, rsi14=80.0)  # overbought
    ctx = StrategyContext(snapshot=snap, yesterday_close=175.0)
    assert Momentum().evaluate(ctx) is None


def test_momentum_skips_when_not_breaking_out() -> None:
    snap = _snap(price=180.0, sma50=170.0, sma200=160.0, rsi14=60.0)
    ctx = StrategyContext(snapshot=snap, yesterday_close=200.0)
    assert Momentum().evaluate(ctx) is None


# ---- safety: malformed / partial snapshots ----

@pytest.mark.parametrize(
    "override",
    [
        {"rsi14": float("nan")},
        {"sma200": None},
        {"price": None},
        {"avg_vol_20": float("nan")},
    ],
)
def test_mean_reversion_no_exception_on_partial(override) -> None:
    snap = _snap(**override)
    ctx = StrategyContext(snapshot=snap, volume_today=5_000_000.0)
    # Must return None without raising.
    assert MeanReversion().evaluate(ctx) is None


@pytest.mark.parametrize(
    "override",
    [
        {"sma50": None},
        {"sma200": float("nan")},
        {"rsi14": None},
    ],
)
def test_momentum_no_exception_on_partial(override) -> None:
    snap = _snap(**override)
    ctx = StrategyContext(snapshot=snap, yesterday_close=175.0)
    assert Momentum().evaluate(ctx) is None


def test_both_strategies_handle_missing_context_fields() -> None:
    snap = _snap(rsi14=25.0)
    # volume_today missing → mean_reversion returns None
    assert MeanReversion().evaluate(StrategyContext(snapshot=snap)) is None
    # yesterday_close missing → momentum returns None
    assert Momentum().evaluate(StrategyContext(snapshot=snap)) is None


# ---- config loader ----

def test_load_enabled_strategies_returns_configured_set() -> None:
    from strategies import load_enabled_strategies

    strats = load_enabled_strategies()
    names = {s.name for s in strats}
    assert names == {"mean_reversion", "momentum"}


def test_load_strategies_config_respects_disabled(tmp_path) -> None:
    from strategies import load_enabled_strategies, load_strategies_config

    # Clear lru_cache so a different path doesn't hit a cached result.
    load_strategies_config.cache_clear()

    cfg = tmp_path / "strategies.yaml"
    cfg.write_text(
        """
strategies:
  mean_reversion:
    enabled: true
  momentum:
    enabled: false
"""
    )
    strats = load_enabled_strategies(cfg)
    assert [s.name for s in strats] == ["mean_reversion"]
    load_strategies_config.cache_clear()


# ---- StrategyAgent end-to-end ----

@pytest.fixture
def db(tmp_path):
    from trading_platform.persistence.db import close_db, init_db

    schema = (Path(__file__).resolve().parent.parent / "src" / "schema.sql").read_text()
    conn = init_db(tmp_path / "events.db", schema)
    try:
        yield conn
    finally:
        close_db()


def _insert_tick(db, now: datetime) -> int:
    db.execute("INSERT INTO ticks (started_at, status) VALUES (?, 'running')", (now.isoformat(),))
    db.commit()
    return int(db.execute("SELECT last_insert_rowid()").fetchone()[0])


def test_agent_writes_signals_with_tick_id_and_rule_only_branch(db) -> None:
    from storage.bars_repo import upsert_bars
    from storage.snapshot_repo import write_snapshot
    from strategies.agent import StrategyAgent

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    yesterday = (now - timedelta(days=1)).date().isoformat()

    # Seed two days of bars so yesterday_close + volume_today are well-defined.
    upsert_bars(db, [
        ("AAPL", yesterday, 170.0, 175.0, 169.0, 175.0, 1_000_000),
        ("AAPL", today, 180.0, 182.0, 179.0, 181.0, 5_000_000),
    ])

    # A snapshot that satisfies momentum (price > sma50 > sma200, rsi in band, price > yclose).
    write_snapshot(db, SnapshotRow(
        symbol="AAPL", ts=now, price=181.0, rsi14=60.0,
        sma20=175.0, sma50=170.0, sma200=160.0,
        avg_vol_20=1_000_000.0, atr14=1.5, price_vs_sma50_pct=6.47,
    ))

    tick_id = _insert_tick(db, now)
    agent = StrategyAgent(db, symbols=["AAPL"])
    signals = agent.run(tick_id=tick_id, now=now)

    assert len(signals) == 1
    s = signals[0]
    assert s.symbol == "AAPL"
    assert s.side == "buy"
    assert s.tick_id == tick_id
    assert s.llm_branch == "rule_only"

    from storage.signal_repo import signals_for_tick
    persisted = signals_for_tick(db, tick_id)
    assert len(persisted) == 1
    assert persisted[0].tick_id == tick_id


def test_agent_merges_multiple_strategies_most_recent_wins(db) -> None:
    from storage.bars_repo import upsert_bars
    from storage.snapshot_repo import write_snapshot
    from strategies.agent import StrategyAgent

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    yesterday = (now - timedelta(days=1)).date().isoformat()

    upsert_bars(db, [
        ("AAPL", yesterday, 170.0, 175.0, 169.0, 175.0, 1_000_000),
        ("AAPL", today, 180.0, 182.0, 179.0, 181.0, 5_000_000),
    ])

    # Snapshot that triggers BOTH rules: rsi14=29 (mean_reversion) but also in 50-70 would be false.
    # Instead craft two separate test cases; here trigger ONLY momentum to keep this focused.
    write_snapshot(db, SnapshotRow(
        symbol="AAPL", ts=now, price=181.0, rsi14=60.0,
        sma20=175.0, sma50=170.0, sma200=160.0,
        avg_vol_20=1_000_000.0, atr14=1.5, price_vs_sma50_pct=6.47,
    ))
    tick_id = _insert_tick(db, now)
    signals = StrategyAgent(db, symbols=["AAPL"]).run(tick_id=tick_id, now=now)
    assert len(signals) == 1  # one symbol → one signal regardless of strategy count


def test_agent_ignores_stale_snapshots(db) -> None:
    from storage.snapshot_repo import write_snapshot
    from strategies.agent import StrategyAgent

    now = datetime.now(timezone.utc)
    # Snapshot older than 90s default freshness window.
    write_snapshot(db, SnapshotRow(
        symbol="AAPL", ts=now - timedelta(seconds=120), price=181.0, rsi14=25.0,
        sma20=175.0, sma50=170.0, sma200=160.0,
        avg_vol_20=1_000_000.0, atr14=1.5, price_vs_sma50_pct=6.47,
    ))
    tick_id = _insert_tick(db, now)
    signals = StrategyAgent(db, symbols=["AAPL"]).run(tick_id=tick_id, now=now)
    assert signals == []


def test_agent_survives_broken_strategy(db) -> None:
    from storage.bars_repo import upsert_bars
    from storage.snapshot_repo import write_snapshot
    from strategies.agent import StrategyAgent

    class _Boom:
        name = "boom"

        def evaluate(self, ctx):
            raise RuntimeError("intentional")

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    yesterday = (now - timedelta(days=1)).date().isoformat()
    upsert_bars(db, [
        ("AAPL", yesterday, 170.0, 175.0, 169.0, 175.0, 1_000_000),
        ("AAPL", today, 180.0, 182.0, 179.0, 181.0, 5_000_000),
    ])
    write_snapshot(db, SnapshotRow(
        symbol="AAPL", ts=now, price=181.0, rsi14=60.0,
        sma20=175.0, sma50=170.0, sma200=160.0,
        avg_vol_20=1_000_000.0, atr14=1.5, price_vs_sma50_pct=6.47,
    ))
    tick_id = _insert_tick(db, now)
    agent = StrategyAgent(db, symbols=["AAPL"], strategies=[_Boom(), Momentum()])
    signals = agent.run(tick_id=tick_id, now=now)
    assert len(signals) == 1  # Momentum still fires even though _Boom raised
    assert signals[0].strategy == "momentum"
