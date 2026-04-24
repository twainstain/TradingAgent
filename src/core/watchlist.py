"""Watchlist loader. Single source is config/watchlist.yaml."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "watchlist.yaml"


@lru_cache(maxsize=8)
def load_watchlist(path: str | Path | None = None) -> tuple[str, ...]:
    """Return the tuple of symbols. Tuple is hashable and cache-friendly."""
    p = Path(path) if path else _DEFAULT_PATH
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    symbols = raw.get("symbols") or []
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        raise ValueError(f"watchlist.yaml must have a list[str] 'symbols' key, got {raw!r}")
    return tuple(s.upper() for s in symbols)
