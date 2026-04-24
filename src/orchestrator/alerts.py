"""Alert wiring — build `AlertDispatcher` from env, adapt to the plain
`alert_hook(event: str, details: dict) -> None` callable shape that the
execution agent and risk agent already use.

Backends used by the dispatcher come from the platform library. Each
backend self-checks `.configured` — if its env vars aren't set it is
simply not added. So turning alerts on is a matter of putting values
in `.env` (Gmail or Telegram).
"""
from __future__ import annotations

import logging
import os
from typing import Callable

from trading_platform.alerting import AlertDispatcher
from trading_platform.alerting.gmail import GmailAlert
from trading_platform.alerting.telegram import TelegramAlert

log = logging.getLogger(__name__)


AlertHook = Callable[[str, dict], None]


def build_dispatcher() -> AlertDispatcher:
    """Return a ready dispatcher with whichever backends are configured."""
    d = AlertDispatcher()
    for backend in (GmailAlert(), TelegramAlert()):
        try:
            d.add_backend(backend)
        except Exception as exc:  # noqa: BLE001 — alerting must never prevent boot
            log.warning("alert backend init failed: %s", exc)
    log.info("alerts: %d backend(s) configured", d.backend_count)
    return d


def hook_from(dispatcher: AlertDispatcher) -> AlertHook:
    """Adapt `AlertDispatcher.alert(event_type, message, details)` to the
    `alert_hook(event, details)` shape used by the execution/risk layers.
    """
    def _hook(event: str, details: dict) -> None:
        message = _format_message(event, details)
        dispatcher.alert(event, message, details)
    return _hook


def _format_message(event: str, details: dict) -> str:
    sym = details.get("symbol") or ""
    head = f"[{event}]" + (f" {sym}" if sym else "")
    if event == "broker_rejection":
        return f"{head} qty={details.get('qty')} error={details.get('error')!s}"
    if event == "fill":
        return (
            f"{head} filled_qty={details.get('filled_qty')} @ {details.get('filled_avg_price')} "
            f"coi={details.get('client_order_id')}"
        )
    if event == "daily_halt_engaged":
        return f"{head} reason={details.get('reason')} pnl={details.get('current_pnl')}"
    if event == "kill_switch_engaged":
        return f"{head} path={details.get('path')}"
    return f"{head} {details!s}"


def null_hook() -> AlertHook:
    """No-op hook used when the user hasn't configured any backend."""
    def _noop(event: str, details: dict) -> None:
        pass
    return _noop
