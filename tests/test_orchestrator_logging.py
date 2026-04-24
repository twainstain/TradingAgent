"""structlog redactor + JSONL sink."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest


def _last_line(p: Path) -> dict:
    lines = [line for line in p.read_text().splitlines() if line]
    return json.loads(lines[-1])


def test_redactor_replaces_known_keys(tmp_path) -> None:
    import orchestrator.logging as olog

    olog.configure(log_dir=tmp_path, trading_date=date(2026, 4, 22))
    log = olog.get_logger("test")
    log.info("boot", api_key="abcd", some_token="xxxxxxxx", event_ok=True)

    rec = _last_line(tmp_path / "2026-04-22.jsonl")
    assert rec["api_key"] == "***REDACTED***"
    assert rec["some_token"] == "***REDACTED***"
    assert rec["event_ok"] is True


def test_redactor_catches_anthropic_key_pattern(tmp_path) -> None:
    import orchestrator.logging as olog

    olog.configure(log_dir=tmp_path, trading_date=date(2026, 4, 22))
    log = olog.get_logger("test")
    fake_ak = "sk-ant-" + "A" * 40
    log.warning("env_leak", my_var=fake_ak)

    rec = _last_line(tmp_path / "2026-04-22.jsonl")
    assert rec["my_var"] == "***REDACTED***"
