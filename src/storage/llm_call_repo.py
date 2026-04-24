"""llm_calls table — per-call audit with tokens/cost/cache-hit/latency."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal

from trading_platform.persistence.db import DbConnection


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def insert_call(
    db: DbConnection,
    *,
    model: str,
    prompt: str,
    response: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: Decimal,
    cache_hit: bool,
    latency_ms: int,
    created_at: datetime | None = None,
) -> int:
    ts = (created_at or datetime.now(timezone.utc)).isoformat()
    cur = db.execute(
        """
        INSERT INTO llm_calls
            (model, prompt_hash, prompt, response, tokens_in, tokens_out,
             cost_usd, cache_hit, latency_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            model,
            prompt_hash(prompt),
            prompt,
            response,
            int(tokens_in),
            int(tokens_out),
            float(cost_usd),
            1 if cache_hit else 0,
            int(latency_ms),
            ts,
        ),
    )
    db.commit()
    return int(cur.lastrowid)


def cost_today(db: DbConnection, trading_date: date) -> Decimal:
    """Sum cost_usd for all calls made on `trading_date` (UTC)."""
    day_start = datetime.combine(trading_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = datetime.combine(trading_date, datetime.max.time(), tzinfo=timezone.utc)
    row = db.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM llm_calls WHERE created_at >= ? AND created_at <= ?",
        (day_start.isoformat(), day_end.isoformat()),
    ).fetchone()
    return Decimal(str(row["total"] or 0))
