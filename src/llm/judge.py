"""Phase-5 LLM judgment stage — Claude Haiku 4.5 (prompt-cached).

Per rule-generated signal: feed the snapshot (features) + up to 3 Polygon
news headlines. Get back JSON `{"approve": bool, "reason": str,
"confidence": float}`. The system prompt and indicators schema are
cached (long-lived, stable text) so subsequent calls are near-free on
input tokens.

Never silently retries. On parse error / API outage / cost cap → returns
the rule-only decision unchanged and sets `llm_branch='rule_only'`.

CLAUDE.md §Model selection: system prompt + indicators schema MUST be
prompt-cached; Haiku 4.5 is the default; MAX_LLM_DAILY_USD is a hard cap.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from trading_platform.persistence.db import DbConnection

from core.signal import Signal
from llm.cost_tracker import DailyCostTracker, estimate_cost
from storage.llm_call_repo import insert_call
from storage.snapshot_repo import SnapshotRow

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL_JUDGE", "claude-haiku-4-5-20251001")

# The system prompt is intentionally stable so it can be fully prompt-cached.
SYSTEM_PROMPT = """You are a conservative risk reviewer for an equities trading bot.

You receive a rule-generated BUY signal plus the underlying snapshot and up
to three news headlines. Reply with ONE JSON object and nothing else:

  {"approve": true|false, "reason": "<one sentence>", "confidence": 0.0-1.0}

Rules for your response:
- If the headlines describe a catalyst that contradicts the signal (e.g.
  an SEC probe, guidance cut, major outage on a mean-reversion bounce),
  approve=false.
- If the signal looks technically clean and headlines are neutral or
  supportive, approve=true.
- NEVER override hard risk limits — you're a sanity check, not a filter
  bypass. The risk agent enforces position size and exposure caps after you.
- No trailing text. No markdown. JSON only.
"""

# Indicators schema, also cached.
INDICATORS_SCHEMA = """Snapshot fields (types, interpretation):
  price: float           last traded price
  rsi14: float           Wilder RSI, 14 days  (0-100)
  sma20/sma50/sma200: float   simple moving averages
  avg_vol_20: float      20-day average daily volume
  atr14: float           Average True Range, 14 days
  price_vs_sma50_pct: float   (price - sma50)/sma50 * 100
Signal fields:
  strategy: "mean_reversion" | "momentum"
  side: "buy" | "sell"
  confidence: 0.0-1.0
  reason: short human-readable rationale
"""


@dataclass(frozen=True)
class JudgeResult:
    approve: bool
    reason: str
    confidence: float
    skipped: bool = False            # True if rule-only (cap/outage/no_client)
    llm_call_id: int | None = None
    branch: str = "rule_only"         # rule_only | llm_approved | llm_rejected


class LLMJudge:
    def __init__(
        self,
        db: DbConnection,
        *,
        client=None,
        cost_tracker: DailyCostTracker | None = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self._db = db
        self._client = client
        self._model = model
        self._cost_tracker = cost_tracker or DailyCostTracker(db)

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from anthropic import Anthropic
        except ImportError:
            return None
        try:
            return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        except KeyError:
            return None

    def _user_message(
        self,
        signal: Signal,
        snapshot: SnapshotRow,
        headlines: list[str],
    ) -> str:
        snap_json = json.dumps(
            {
                "symbol": snapshot.symbol,
                "price": snapshot.price,
                "rsi14": snapshot.rsi14,
                "sma20": snapshot.sma20,
                "sma50": snapshot.sma50,
                "sma200": snapshot.sma200,
                "avg_vol_20": snapshot.avg_vol_20,
                "atr14": snapshot.atr14,
                "price_vs_sma50_pct": snapshot.price_vs_sma50_pct,
            },
            default=str,
        )
        sig_json = json.dumps(
            {
                "strategy": signal.strategy,
                "side": signal.side,
                "confidence": signal.confidence,
                "reason": signal.reason,
            }
        )
        # Only include headlines block if non-empty — stable cache-prefix otherwise.
        headlines_block = ""
        if headlines:
            headlines_block = "\n\nHeadlines:\n" + "\n".join(f"- {h}" for h in headlines[:3])
        return f"Signal: {sig_json}\nSnapshot: {snap_json}{headlines_block}"

    def _parse(self, response_text: str) -> tuple[bool, str, float] | None:
        text = response_text.strip()
        # Strip possible ```json fences
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            obj = json.loads(text)
        except Exception:  # noqa: BLE001
            return None
        try:
            approve = bool(obj["approve"])
            reason = str(obj.get("reason", ""))
            confidence = float(obj.get("confidence", 0.0))
        except (KeyError, TypeError, ValueError):
            return None
        return approve, reason, max(0.0, min(1.0, confidence))

    def judge(
        self,
        *,
        signal: Signal,
        snapshot: SnapshotRow,
        headlines: list[str] | None = None,
        trading_date: date | None = None,
    ) -> JudgeResult:
        today = trading_date or date.today()
        if self._cost_tracker.is_over_cap(today):
            log.info("llm cost cap hit for %s — skipping judgment, rule-only", today)
            return JudgeResult(approve=True, reason="cost_cap_hit", confidence=0.0,
                               skipped=True, branch="rule_only")

        client = self._get_client()
        if client is None:
            return JudgeResult(approve=True, reason="no_llm_client", confidence=0.0,
                               skipped=True, branch="rule_only")

        user_msg = self._user_message(signal, snapshot, headlines or [])
        # Build cached system blocks (Anthropic prompt caching).
        system_blocks = [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": INDICATORS_SCHEMA, "cache_control": {"type": "ephemeral"}},
        ]

        t0 = time.monotonic()
        try:
            resp = client.messages.create(
                model=self._model,
                max_tokens=200,
                system=system_blocks,
                messages=[{"role": "user", "content": user_msg}],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("llm outage — rule-only: %s", exc)
            return JudgeResult(approve=True, reason=f"llm_outage:{exc}", confidence=0.0,
                               skipped=True, branch="rule_only")
        latency_ms = int((time.monotonic() - t0) * 1000)

        # Extract text + usage
        response_text = "".join(
            getattr(b, "text", "") for b in getattr(resp, "content", [])
        ) or ""
        usage = getattr(resp, "usage", None)
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_hit = cache_read > 0

        est = estimate_cost(tokens_in, tokens_out, cache_hit=cache_hit)
        call_id = insert_call(
            self._db,
            model=self._model,
            prompt=user_msg,
            response=response_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=est.cost_usd,
            cache_hit=cache_hit,
            latency_ms=latency_ms,
        )

        parsed = self._parse(response_text)
        if parsed is None:
            log.warning("llm response unparseable — rule-only: %r", response_text[:120])
            return JudgeResult(approve=True, reason="unparseable", confidence=0.0,
                               skipped=True, llm_call_id=call_id, branch="rule_only")

        approve, reason, confidence = parsed
        return JudgeResult(
            approve=approve,
            reason=reason,
            confidence=confidence,
            skipped=False,
            llm_call_id=call_id,
            branch="llm_approved" if approve else "llm_rejected",
        )
