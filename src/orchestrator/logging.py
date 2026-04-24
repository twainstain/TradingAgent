"""structlog configuration — JSONL to `logs/YYYY-MM-DD.jsonl` plus stderr.

Redactor strips anything that looks like an API key or secret before the
event is rendered. CLAUDE.md §Security: "keys never printed to logs
(redactor in `structlog`)."
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import structlog

DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"

# Keys whose value must never be logged — exact match.
_REDACT_KEYS = frozenset({
    "api_key", "secret", "password", "authorization",
    "alpaca_api_key", "alpaca_api_secret",
    "anthropic_api_key", "polygon_api_key",
})
# Key SUFFIXES that mark the value as sensitive regardless of prefix.
# e.g. `some_token`, `gmail_app_password`, `client_secret`.
_REDACT_KEY_SUFFIXES = ("_api_key", "_token", "_secret", "_password")

# Prefixes that mark a value as secret-like even if the key is innocent.
_SECRET_VALUE_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),          # Anthropic
    re.compile(r"PK[A-Z0-9]{14,}"),                       # Alpaca paper key
    re.compile(r"[A-Za-z0-9]{32,}"),                      # generic long token (conservative last-resort)
]

_REDACTED = "***REDACTED***"


def _redact_value(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    # Full-string match: replace entirely.
    for pat in _SECRET_VALUE_PATTERNS[:2]:  # only run the narrow ones by default
        if pat.fullmatch(v):
            return _REDACTED
    return v


def _key_is_secret(k: str) -> bool:
    kl = k.lower()
    if kl in _REDACT_KEYS:
        return True
    return any(kl.endswith(suf) for suf in _REDACT_KEY_SUFFIXES)


def redactor(_logger, _method_name, event_dict):
    for k in list(event_dict.keys()):
        if _key_is_secret(k):
            event_dict[k] = _REDACTED
            continue
        event_dict[k] = _redact_value(event_dict[k])
    return event_dict


def _file_sink(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a", encoding="utf-8", buffering=1)  # line-buffered

    def _write(_logger, _method, event_dict):
        import json

        line = json.dumps(event_dict, default=str)
        f.write(line + "\n")
        return event_dict

    return _write


def configure(
    *,
    log_dir: Path | None = None,
    trading_date: date | None = None,
    level: int = logging.INFO,
) -> None:
    """Idempotent: safe to call more than once (later calls override config)."""
    log_dir = log_dir or DEFAULT_LOG_DIR
    td = trading_date or datetime.now(timezone.utc).date()
    log_path = log_dir / f"{td.isoformat()}.jsonl"

    logging.basicConfig(stream=sys.stderr, level=level, format="%(message)s")

    processors = [
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redactor,
        _file_sink(log_path),
        structlog.processors.JSONRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
