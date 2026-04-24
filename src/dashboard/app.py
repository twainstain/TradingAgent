"""Dashboard — FastAPI + Jinja, read-only window onto events.db.

Views (ARCHITECTURE §3.7):
  GET /            → Today (open orders, today's fills, halt + kill status)
  GET /signals     → Signals (symbol/strategy filters + risk + order info)
  GET /latency     → Per-stage p50/p95 from logs/latency.jsonl
  GET /daily       → Renders logs/daily_summary_YYYY-MM-DD.md
  GET /admin       → risk_state table + kill-switch confirm form
  POST /admin/kill-switch/engage → writes data/KILL. LOCALHOST ONLY.
  GET /health      → Phase 0 smoke (retained)

Security invariants:
  - DB opened with `sqlite:///…?mode=ro` (URI form). Write attempts raise.
  - Kill-switch endpoint refuses non-loopback origins (127.0.0.1 / ::1).
  - No broker credentials in this process's env (docker-compose.yml mounts
    data + logs read-only for the dashboard service).
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard.db import open_readonly
from orchestrator.daily_summary import _latency_percentiles  # reuse internal helper

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT / "data" / "events.db"
DEFAULT_KILL_PATH = ROOT / "data" / "KILL"
DEFAULT_LATENCY_PATH = ROOT / "logs" / "latency.jsonl"
DEFAULT_LOG_DIR = ROOT / "logs"


def _db_path() -> Path:
    return Path(os.environ.get("EVENTS_DB", str(DEFAULT_DB_PATH)))


def _kill_path() -> Path:
    return Path(os.environ.get("KILL_SWITCH_PATH", str(DEFAULT_KILL_PATH)))


def _latency_path() -> Path:
    return Path(os.environ.get("LATENCY_PATH", str(DEFAULT_LATENCY_PATH)))


def _log_dir() -> Path:
    return Path(os.environ.get("LOG_DIR", str(DEFAULT_LOG_DIR)))


def _today_et_str() -> str:
    # Use UTC date; dashboard is informational, not market-clock-critical.
    return datetime.now(timezone.utc).date().isoformat()


def _day_bounds(d: date) -> tuple[str, str]:
    s = datetime.combine(d, time.min, tzinfo=timezone.utc).isoformat()
    e = datetime.combine(d, time.max, tzinfo=timezone.utc).isoformat()
    return s, e


def _is_loopback(host: str | None) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


app = FastAPI(title="trading-agent dashboard", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ---------------------------------------------------------------------------
# Health (Phase 0)
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


# ---------------------------------------------------------------------------
# Today
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def today(request: Request) -> HTMLResponse:
    td = date.fromisoformat(_today_et_str())
    s_iso, e_iso = _day_bounds(td)
    db = open_readonly(_db_path())
    try:
        risk_state = db.execute(
            "SELECT * FROM risk_state WHERE trading_date = ?",
            (td.isoformat(),),
        ).fetchone()
        fills = db.execute(
            """
            SELECT f.reported_at, f.filled_qty, f.filled_avg_price, f.status,
                   o.symbol, o.side
            FROM fills f JOIN orders o ON o.id = f.order_id
            WHERE f.reported_at >= ? AND f.reported_at <= ?
            ORDER BY f.reported_at DESC
            """,
            (s_iso, e_iso),
        ).fetchall()
        open_orders = db.execute(
            """
            SELECT submitted_at, symbol, side, qty, entry_price, stop_price,
                   take_profit_price, status
            FROM orders
            WHERE status NOT IN ('filled','canceled','cancelled','expired','rejected','replaced','done_for_day')
            ORDER BY submitted_at DESC
            """,
        ).fetchall()
    finally:
        db.close()
    return templates.TemplateResponse(
        request,
        "today.html",
        {
            "nav": "today", "title": "Today",
            "trading_date": td.isoformat(),
            "risk_state": dict(risk_state) if risk_state else None,
            "fills": [dict(r) for r in fills],
            "open_orders": [dict(r) for r in open_orders],
            "kill_switch_engaged": _kill_path().is_file(),
        },
    )


# ---------------------------------------------------------------------------
# Signals (filterable trace)
# ---------------------------------------------------------------------------
@app.get("/signals", response_class=HTMLResponse)
def signals(request: Request, date: str | None = None, symbol: str | None = None, strategy: str | None = None) -> HTMLResponse:
    d = date or _today_et_str()
    try:
        td = datetime.fromisoformat(d).date()
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    s_iso, e_iso = _day_bounds(td)
    params: list = [s_iso, e_iso]
    where = "s.created_at >= ? AND s.created_at <= ?"
    if symbol:
        where += " AND s.symbol = ?"
        params.append(symbol.upper())
    if strategy:
        where += " AND s.strategy = ?"
        params.append(strategy)
    db = open_readonly(_db_path())
    try:
        rows = db.execute(
            f"""
            SELECT s.id, s.created_at, s.symbol, s.strategy, s.side, s.confidence,
                   s.reason, s.llm_branch,
                   rd.approved AS risk_approved, rd.reason AS risk_reason, rd.sized_qty AS risk_qty,
                   o.client_order_id, o.status AS order_status
            FROM signals s
            LEFT JOIN risk_decisions rd ON rd.signal_id = s.id
            LEFT JOIN orders o ON o.risk_decision_id = rd.id
            WHERE {where}
            ORDER BY s.id DESC
            """,
            tuple(params),
        ).fetchall()
    finally:
        db.close()
    return templates.TemplateResponse(
        request,
        "signals.html",
        {
            "nav": "signals", "title": "Signals",
            "signals": [dict(r) for r in rows],
            "filters": {"date": td.isoformat(), "symbol": symbol, "strategy": strategy},
        },
    )


# ---------------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------------
@app.get("/latency", response_class=HTMLResponse)
def latency(request: Request, date: str | None = None) -> HTMLResponse:
    d = date or _today_et_str()
    try:
        td = datetime.fromisoformat(d).date()
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    stages = _latency_percentiles(_latency_path(), td)
    return templates.TemplateResponse(
        request,
        "latency.html",
        {"nav": "latency", "title": "Latency",
         "trading_date": td.isoformat(), "stages": stages},
    )


# ---------------------------------------------------------------------------
# Daily summary (markdown file)
# ---------------------------------------------------------------------------
@app.get("/daily", response_class=HTMLResponse)
def daily(request: Request, date: str | None = None) -> HTMLResponse:
    d = date or _today_et_str()
    try:
        td = datetime.fromisoformat(d).date()
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    path = _log_dir() / f"daily_summary_{td.isoformat()}.md"
    md = path.read_text(encoding="utf-8") if path.exists() else ""
    return templates.TemplateResponse(
        request,
        "daily.html",
        {"nav": "daily", "title": "Daily summary",
         "trading_date": td.isoformat(), "markdown": md},
    )


# ---------------------------------------------------------------------------
# Admin (risk_state viewer + kill-switch confirm)
# ---------------------------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request) -> HTMLResponse:
    db = open_readonly(_db_path())
    try:
        states = db.execute("SELECT * FROM risk_state ORDER BY trading_date DESC LIMIT 30").fetchall()
    finally:
        db.close()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"nav": "admin", "title": "Admin",
         "states": [dict(s) for s in states],
         "kill_switch_engaged": _kill_path().is_file()},
    )


@app.post("/admin/kill-switch/engage")
async def engage_kill_switch(request: Request) -> RedirectResponse:
    """Create data/KILL. ONLY WRITE PATH IN THIS DASHBOARD.

    Must come from a loopback address. Must carry the 'confirm=ENGAGE' field.
    """
    client_host = request.client.host if request.client else None
    if not _is_loopback(client_host):
        raise HTTPException(403, "kill switch can only be engaged from localhost")
    form = await request.form()
    if form.get("confirm") != "ENGAGE":
        raise HTTPException(400, "must type ENGAGE to confirm")
    path = _kill_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"engaged_at={datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8"
    )
    return RedirectResponse("/admin", status_code=303)
