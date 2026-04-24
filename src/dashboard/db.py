"""Read-only SQLite connection for the dashboard.

The dashboard is a read-only window onto `data/events.db`. ANY write
attempt must raise (ARCHITECTURE §3.7). Opened via URI with `mode=ro`
so sqlite rejects DML/DDL at the driver level.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path).resolve()
    # URI form; `mode=ro` → sqlite rejects INSERT/UPDATE/DELETE/CREATE at the driver level.
    uri = f"file:{p}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn
