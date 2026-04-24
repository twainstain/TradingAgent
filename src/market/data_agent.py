"""Data Agent — per-tick pipeline: load bars → compute features → write snapshot.

This module owns:
  - Startup backfill of `bars_daily` (via alpaca_rest.backfill_bars_daily)
  - The live in-process latest-snapshot cache (TTLCache, 90s)
  - Per-tick feature recomputation and write-through to `snapshots`
  - Retention pruning of `snapshots` older than 2 trading days
  - Latency marks around each stage

The websocket loop itself is driven by the Orchestrator (Phase 5). For now,
the Data Agent exposes a sync `run_tick(price_map)` that the orchestrator
(or `scripts/replay.py` in Phase 2) can call with the latest prices per
symbol. That keeps this module testable without a live websocket.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from trading_platform.data import TTLCache
from trading_platform.observability import LatencyTracker
from trading_platform.persistence.db import DbConnection

from market.indicators import compute_features
from storage.bars_repo import load_bars
from storage.snapshot_repo import SnapshotRow, prune_older_than, write_snapshots

log = logging.getLogger(__name__)

SNAPSHOT_CACHE_TTL_SECONDS = 90.0
RETENTION_DAYS = 2


class DataAgent:
    def __init__(
        self,
        db: DbConnection,
        symbols: Iterable[str],
        *,
        latency_tracker: LatencyTracker | None = None,
        cache_ttl_seconds: float = SNAPSHOT_CACHE_TTL_SECONDS,
        retention_days: int = RETENTION_DAYS,
    ) -> None:
        self._db = db
        self._symbols = tuple(s.upper() for s in symbols)
        self._cache = TTLCache(ttl_seconds=cache_ttl_seconds)
        self._tracker = latency_tracker
        self._retention_days = retention_days

    @property
    def cache(self) -> TTLCache:
        return self._cache

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    def run_tick(
        self,
        price_map: dict[str, float],
        *,
        tick_id: int | None = None,
        now: datetime | None = None,
    ) -> list[SnapshotRow]:
        """Compute features for every symbol with a price, persist, cache.

        `price_map` is the map of latest prices (from the websocket, or
        synthetic for replay). Symbols missing from the map are skipped.
        """
        now = now or datetime.now(timezone.utc)
        if self._tracker is not None:
            self._tracker.start_cycle()

        rows: list[SnapshotRow] = []
        for sym in self._symbols:
            price = price_map.get(sym)
            if price is None:
                continue
            bars = load_bars(self._db, sym, lookback_days=max(self._retention_days * 200, 300))
            if bars.empty:
                log.warning("no bars for %s; skipping feature compute", sym)
                continue
            feats = compute_features(bars, latest_price=price)
            rows.append(
                SnapshotRow(
                    tick_id=tick_id,
                    symbol=sym,
                    ts=now,
                    price=feats.price,
                    rsi14=feats.rsi14,
                    sma20=feats.sma20,
                    sma50=feats.sma50,
                    sma200=feats.sma200,
                    avg_vol_20=feats.avg_vol_20,
                    atr14=feats.atr14,
                    price_vs_sma50_pct=feats.price_vs_sma50_pct,
                )
            )

        if self._tracker is not None:
            self._tracker.mark("indicators_ms")

        write_snapshots(self._db, rows)

        if self._tracker is not None:
            self._tracker.mark("snapshot_write_ms")

        # Write-through cache update after the DB commit succeeds.
        for row in rows:
            self._cache.set(row.symbol, row, reason="tick_snapshot")

        # Retention prune — per tick, keep snapshots bounded.
        prune_cutoff = now - timedelta(days=self._retention_days)
        deleted = prune_older_than(self._db, prune_cutoff)
        if deleted:
            log.debug("pruned %d old snapshot rows", deleted)

        if self._tracker is not None:
            self._tracker.mark("retention_ms")

        return rows

    def record_quote(self, symbol: str, price: float, ts: datetime | None = None) -> None:
        """Fast path for websocket callbacks: update cache only.

        This doesn't re-run indicators (too expensive per-quote) — it just
        keeps the "latest observed price" in the TTLCache so the next tick
        can feed it into run_tick. The orchestrator drives tick cadence.
        """
        ts = ts or datetime.now(timezone.utc)
        self._cache.set(
            f"price:{symbol.upper()}",
            (ts, float(price)),
            reason="ws_quote",
        )

    def latest_price(self, symbol: str) -> float | None:
        cached = self._cache.get(f"price:{symbol.upper()}")
        if not cached:
            return None
        _ts, price = cached  # type: ignore[misc]
        return price

    def price_map(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for sym in self._symbols:
            p = self.latest_price(sym)
            if p is not None:
                out[sym] = p
        return out
