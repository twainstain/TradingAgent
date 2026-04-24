"""Phase 5: LLM judge — happy path, parse failure, API outage, A/B branch."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.signal import Signal
from storage.snapshot_repo import SnapshotRow


@pytest.fixture
def db(tmp_path):
    from trading_platform.persistence.db import close_db, init_db

    schema = (Path(__file__).resolve().parent.parent / "src" / "schema.sql").read_text()
    conn = init_db(tmp_path / "events.db", schema)
    try:
        yield conn
    finally:
        close_db()


def _signal() -> Signal:
    return Signal(symbol="AAPL", side="buy", strategy="mean_reversion",
                  confidence=1.0, reason="t", tick_id=1)


def _snap() -> SnapshotRow:
    return SnapshotRow(symbol="AAPL", ts=datetime.now(timezone.utc), price=180.0,
                      rsi14=55.0, sma20=175.0, sma50=170.0, sma200=160.0,
                      avg_vol_20=3e6, atr14=1.5, price_vs_sma50_pct=5.88)


class _Resp:
    def __init__(self, text: str, tokens_in=120, tokens_out=30, cache_read=0):
        self.content = [SimpleNamespace(text=text)]
        self.usage = SimpleNamespace(
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cache_read_input_tokens=cache_read,
        )


class _Client:
    def __init__(self, text: str, raise_exc: Exception | None = None):
        self._text = text
        self._raise = raise_exc

        class _Messages:
            @staticmethod
            def create(**_kwargs):
                if raise_exc is not None:
                    raise raise_exc
                return _Resp(text)

        self.messages = _Messages()


def test_judge_approves_and_writes_llm_call(db) -> None:
    from llm.judge import LLMJudge

    client = _Client('{"approve": true, "reason": "trend clean", "confidence": 0.8}')
    res = LLMJudge(db, client=client).judge(
        signal=_signal(), snapshot=_snap(), trading_date=date(2026, 4, 22),
    )
    assert res.approve is True
    assert res.branch == "llm_approved"
    assert res.confidence == 0.8
    assert res.llm_call_id is not None

    row = db.execute("SELECT model, tokens_in, tokens_out, cache_hit FROM llm_calls WHERE id = ?",
                     (res.llm_call_id,)).fetchone()
    assert row["tokens_in"] == 120
    assert row["tokens_out"] == 30


def test_judge_rejects_sets_llm_rejected_branch(db) -> None:
    from llm.judge import LLMJudge

    client = _Client('{"approve": false, "reason": "SEC probe", "confidence": 0.9}')
    res = LLMJudge(db, client=client).judge(
        signal=_signal(), snapshot=_snap(), trading_date=date(2026, 4, 22),
    )
    assert res.approve is False
    assert res.branch == "llm_rejected"


def test_judge_unparseable_falls_back_to_rule_only(db) -> None:
    from llm.judge import LLMJudge

    client = _Client("not json at all")
    res = LLMJudge(db, client=client).judge(
        signal=_signal(), snapshot=_snap(), trading_date=date(2026, 4, 22),
    )
    assert res.skipped is True
    assert res.branch == "rule_only"
    assert res.approve is True  # when skipped, we pass through (rule-only)


def test_judge_api_outage_falls_back(db) -> None:
    from llm.judge import LLMJudge

    client = _Client("", raise_exc=RuntimeError("anthropic down"))
    res = LLMJudge(db, client=client).judge(
        signal=_signal(), snapshot=_snap(), trading_date=date(2026, 4, 22),
    )
    assert res.skipped is True
    assert res.branch == "rule_only"
    assert "llm_outage" in res.reason


def test_judge_fenced_json_is_parsed(db) -> None:
    from llm.judge import LLMJudge

    client = _Client('```json\n{"approve": true, "reason": "ok", "confidence": 0.5}\n```')
    res = LLMJudge(db, client=client).judge(
        signal=_signal(), snapshot=_snap(), trading_date=date(2026, 4, 22),
    )
    assert res.approve is True
    assert res.branch == "llm_approved"
