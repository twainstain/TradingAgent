"""Phase 2 dry-run harness.

Reads `bars_daily` (which must already be backfilled — see Phase 1 alpaca_rest),
for each symbol in the watchlist reconstructs features as of the `--date`, runs
the enabled strategies, and prints any signals to stdout.

This is a **read-only** replay — it does not write snapshots or signals to
the DB. Use it to eyeball-review rule output before Phase 4 execution is wired.

Example:
    PYTHONPATH=src python scripts/replay.py --date 2026-04-22
    PYTHONPATH=src python scripts/replay.py --date 2026-04-22 --symbols AAPL,MSFT

Note on "per-minute ticks": daily bars don't give us intraday granularity, so
this harness runs ONE tick per date using the day's close as the latest price.
The plan's mention of per-minute replay is aspirational and will land when we
have intraday bar history (Phase 7).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

# sys.path wiring (scripts/ runs outside the installed package)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import _bootstrap  # noqa: F401

from core.watchlist import load_watchlist
from market.indicators import compute_features
from storage.bars_repo import load_bars
from storage.snapshot_repo import SnapshotRow
from strategies import load_enabled_strategies
from strategies.base import StrategyContext

from trading_platform.persistence.db import close_db, init_db


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _as_of_datetime(d: date) -> datetime:
    # Treat the replay tick as 16:00 UTC on `d` (approx post-close ET).
    return datetime.combine(d, time(16, 0, tzinfo=timezone.utc))


def _truncate_bars_to(bars, replay_date: date):
    cutoff = bars.index <= str(replay_date)
    return bars.loc[cutoff]


def run_replay(db, replay_date: date, symbols: list[str]) -> int:
    strategies = load_enabled_strategies()
    as_of = _as_of_datetime(replay_date)
    n_signals = 0

    for sym in symbols:
        all_bars = load_bars(db, sym)
        bars = _truncate_bars_to(all_bars, replay_date)
        if bars.empty:
            print(f"[{sym}] no bars up to {replay_date} — skipping")
            continue
        if str(bars.index[-1].date()) != str(replay_date):
            print(f"[{sym}] no bar for exact date {replay_date} — latest is {bars.index[-1].date()}")
            continue

        close_today = float(bars["close"].iloc[-1])
        volume_today = float(bars["volume"].iloc[-1])
        yesterday_close = float(bars["close"].iloc[-2]) if len(bars) >= 2 else None

        feats = compute_features(bars, latest_price=close_today)
        snap = SnapshotRow(
            symbol=sym,
            ts=as_of,
            price=feats.price,
            rsi14=feats.rsi14,
            sma20=feats.sma20,
            sma50=feats.sma50,
            sma200=feats.sma200,
            avg_vol_20=feats.avg_vol_20,
            atr14=feats.atr14,
            price_vs_sma50_pct=feats.price_vs_sma50_pct,
        )
        ctx = StrategyContext(
            snapshot=snap,
            volume_today=volume_today,
            yesterday_close=yesterday_close,
        )

        sym_signals = []
        for strat in strategies:
            try:
                sig = strat.evaluate(ctx)
            except Exception as exc:  # noqa: BLE001
                print(f"[{sym}] {strat.name} raised: {exc}")
                continue
            if sig is not None:
                sym_signals.append(sig)

        if not sym_signals:
            print(
                f"[{sym}] no signals | price={snap.price:.2f} rsi14={snap.rsi14:.1f} "
                f"sma50={snap.sma50:.2f} sma200={snap.sma200:.2f} "
                f"vol_today={volume_today:,.0f} avg_vol_20={snap.avg_vol_20:,.0f}"
            )
            continue

        n_signals += len(sym_signals)
        for sig in sym_signals:
            print(f"[{sym}] SIGNAL {sig.side.upper()} via {sig.strategy} :: {sig.reason}")

    return n_signals


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", required=True, type=_parse_date, help="replay date (YYYY-MM-DD)")
    ap.add_argument(
        "--db",
        default=str(Path(__file__).resolve().parent.parent / "data" / "events.db"),
        help="path to events.db (default: data/events.db)",
    )
    ap.add_argument(
        "--symbols",
        default=None,
        help="comma-separated symbols (default: config/watchlist.yaml)",
    )
    args = ap.parse_args(argv)

    schema = (Path(__file__).resolve().parent.parent / "src" / "schema.sql").read_text()
    db = init_db(args.db, schema)
    try:
        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        else:
            symbols = list(load_watchlist())
        n = run_replay(db, args.date, symbols)
        print(f"\n=== replay {args.date} complete: {n} signals across {len(symbols)} symbols ===")
        return 0
    finally:
        close_db()


if __name__ == "__main__":
    raise SystemExit(main())
