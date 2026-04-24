"""Phase 0 stub: only /health. Real views land in Phase 5b.

The dashboard opens SQLite read-only and has no broker creds. It is a
separate process from the agent; the only write path it will ever gain is
the kill-switch button (creates data/KILL), which lands in Phase 5b.
"""
from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="trading-agent dashboard", version="0.0.0")


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
