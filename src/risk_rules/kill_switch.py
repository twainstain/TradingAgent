"""Kill switch — presence of `data/KILL` (or `/app/data/KILL` in-container)
rejects all signals and emits a single FLATTEN_ALL.

Deliberately a local file (CLAUDE.md invariant #4): no API, no remote
flag. The dashboard's only write path will be this file (Phase 5b).
"""
from __future__ import annotations

from pathlib import Path

DEFAULT_KILL_PATH = Path(__file__).resolve().parents[2] / "data" / "KILL"


def kill_switch_path() -> Path:
    return DEFAULT_KILL_PATH


def is_engaged(path: Path | str | None = None) -> bool:
    p = Path(path) if path else DEFAULT_KILL_PATH
    return p.is_file()


FLATTEN_ALL_SIGNAL = "FLATTEN_ALL"
