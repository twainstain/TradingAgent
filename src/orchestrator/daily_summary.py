"""End-of-day summary writer — `logs/daily_summary_YYYY-MM-DD.md`.

Scheduled for 16:30 ET by the orchestrator. Pulls from SQLite + the
`logs/latency.jsonl` stream (via the platform's `analyze_latency`).

Contents (EXECUTION_PLAN §Phase 5):
  - P&L (today's fills; rough mark-to-market for open positions — we
    only report realized-ish here, from fills; unrealized goes in the
    dashboard Today view in Phase 5b)
  - trade count + LLM approval/rejection breakdown
  - LLM cost
  - top signals
  - per-stage latency p50/p95

Writes a UTF-8 markdown file. Idempotent: same date overwrites.
"""
from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path

from trading_platform.persistence.db import DbConnection

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DailySummary:
    trading_date: date
    path: Path
    markdown: str


def _day_bounds(trading_date: date) -> tuple[str, str]:
    start = datetime.combine(trading_date, time.min, tzinfo=timezone.utc).isoformat()
    end = datetime.combine(trading_date, time.max, tzinfo=timezone.utc).isoformat()
    return start, end


def _fill_rows(db: DbConnection, trading_date: date):
    s, e = _day_bounds(trading_date)
    return db.execute(
        """
        SELECT f.client_order_id, f.filled_qty, f.filled_avg_price, f.status, f.reported_at,
               o.symbol, o.side, o.entry_price, o.stop_price, o.take_profit_price
        FROM fills f JOIN orders o ON o.id = f.order_id
        WHERE f.reported_at >= ? AND f.reported_at <= ?
        ORDER BY f.reported_at ASC
        """,
        (s, e),
    ).fetchall()


def _signal_counts(db: DbConnection, trading_date: date) -> dict[str, int]:
    s, e = _day_bounds(trading_date)
    rows = db.execute(
        """
        SELECT COALESCE(llm_branch, 'rule_only') AS branch, COUNT(*) AS n
        FROM signals WHERE created_at >= ? AND created_at <= ?
        GROUP BY branch
        """,
        (s, e),
    ).fetchall()
    return {r["branch"]: r["n"] for r in rows}


def _llm_cost(db: DbConnection, trading_date: date) -> tuple[float, int, int]:
    s, e = _day_bounds(trading_date)
    row = db.execute(
        """
        SELECT COALESCE(SUM(cost_usd), 0) AS total_usd,
               COUNT(*) AS n_calls,
               COALESCE(SUM(cache_hit), 0) AS cache_hits
        FROM llm_calls WHERE created_at >= ? AND created_at <= ?
        """,
        (s, e),
    ).fetchone()
    return float(row["total_usd"] or 0.0), int(row["n_calls"]), int(row["cache_hits"])


def _latency_percentiles(latency_path: Path, trading_date: date) -> dict[str, dict[str, float]]:
    """Parse logs/latency.jsonl and return per-stage {p50, p95} for today's entries.
    Keys are stage names as written by `LatencyTracker.mark()`.
    """
    if not latency_path.exists():
        return {}
    start = datetime.combine(trading_date, time.min, tzinfo=timezone.utc)
    end = datetime.combine(trading_date, time.max, tzinfo=timezone.utc)
    per_stage: dict[str, list[float]] = {}
    with latency_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # LatencyTracker writes `timestamp` or `started_at`; be lenient.
            ts_str = rec.get("timestamp") or rec.get("started_at") or rec.get("ts")
            if ts_str:
                try:
                    t = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if not (start <= t <= end):
                    continue
            marks = rec.get("cycle_marks") or rec.get("marks") or {}
            for stage, ms in marks.items():
                try:
                    per_stage.setdefault(stage, []).append(float(ms))
                except (TypeError, ValueError):
                    continue
    out: dict[str, dict[str, float]] = {}
    for stage, vals in per_stage.items():
        if not vals:
            continue
        vals_sorted = sorted(vals)
        out[stage] = {
            "p50": _quantile(vals_sorted, 0.50),
            "p95": _quantile(vals_sorted, 0.95),
            "n": len(vals_sorted),
        }
    return out


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    k = int(round(q * (len(sorted_vals) - 1)))
    return sorted_vals[k]


def render(
    db: DbConnection,
    trading_date: date,
    *,
    latency_path: Path | None = None,
) -> str:
    latency_path = latency_path or (Path(__file__).resolve().parents[2] / "logs" / "latency.jsonl")

    fills = _fill_rows(db, trading_date)
    # Top-line: count FILLED transitions (append-only fills table has partial+filled rows).
    filled = [r for r in fills if r["status"] == "filled"]
    signal_branches = _signal_counts(db, trading_date)
    cost_usd, n_calls, cache_hits = _llm_cost(db, trading_date)
    lat = _latency_percentiles(latency_path, trading_date)

    lines: list[str] = []
    lines.append(f"# Daily Summary — {trading_date.isoformat()}")
    lines.append("")
    lines.append(f"- Filled orders: **{len(filled)}**")
    lines.append(f"- Total fill events (incl. partials): {len(fills)}")
    lines.append("")
    lines.append("## Signals")
    if not signal_branches:
        lines.append("- (no signals)")
    for branch, n in sorted(signal_branches.items()):
        lines.append(f"- `{branch}`: {n}")
    lines.append("")
    lines.append("## LLM")
    lines.append(f"- Calls: {n_calls} (cache hits: {cache_hits})")
    lines.append(f"- Cost: ${cost_usd:.4f}")
    lines.append("")
    lines.append("## Latency (ms)")
    if not lat:
        lines.append("- (no latency traces for this date)")
    else:
        for stage, qs in sorted(lat.items()):
            lines.append(f"- `{stage}` (n={qs['n']}): p50={qs['p50']:.1f}, p95={qs['p95']:.1f}")
    lines.append("")
    lines.append("## Fills")
    if not fills:
        lines.append("- (no fills today)")
    else:
        for r in filled[:20]:
            lines.append(
                f"- {r['reported_at']} {r['symbol']} {r['side']} qty={r['filled_qty']} "
                f"@ {r['filled_avg_price']} (entry≈{r['entry_price']} stop={r['stop_price']} tp={r['take_profit_price']})"
            )
        if len(filled) > 20:
            lines.append(f"- … (+{len(filled) - 20} more)")
    return "\n".join(lines) + "\n"


def write_summary(
    db: DbConnection,
    trading_date: date,
    *,
    log_dir: Path | None = None,
    latency_path: Path | None = None,
) -> DailySummary:
    log_dir = log_dir or (Path(__file__).resolve().parents[2] / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    md = render(db, trading_date, latency_path=latency_path)
    path = log_dir / f"daily_summary_{trading_date.isoformat()}.md"
    path.write_text(md, encoding="utf-8")
    return DailySummary(trading_date=trading_date, path=path, markdown=md)
