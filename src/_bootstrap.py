"""Sys.path bootstrap for scripts run outside the installed package.

Usage from scripts/*.py (which live outside `src/`):

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    import _bootstrap  # noqa: F401  -- extends sys.path to include lib/trading_platform/src

After this import, `src/` top-level modules (e.g. `market`, `strategies`,
`dashboard`) and platform modules (`pipeline`, `risk`, ...) are importable.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

for _p in (
    _REPO_ROOT / "src",
    _REPO_ROOT / "lib" / "trading_platform" / "src",
):
    _s = str(_p)
    if _p.exists() and _s not in sys.path:
        sys.path.insert(0, _s)
