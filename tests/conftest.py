"""Pytest bootstrap: wire src/, lib/trading_platform/src/, and scripts/ onto sys.path.

Lets `pytest tests/` work without requiring `pip install -e .` for the project
or its submodule.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

for _p in (
    _ROOT / "src",
    _ROOT / "lib" / "trading_platform" / "src",
    _ROOT / "scripts",
):
    _s = str(_p)
    if _p.exists() and _s not in sys.path:
        sys.path.insert(0, _s)
