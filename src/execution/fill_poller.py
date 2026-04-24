"""Fill poller — `/v2/orders` via alpaca-py → `fills` table.

Paper endpoint has no public webhook ingress, so we poll. Called once per
orchestrator tick (Phase 5). For each client_order_id in `orders` whose
status isn't yet terminal, we fetch the live broker state and upsert a
new row into `fills` if anything changed.

Terminal statuses (per alpaca-py `OrderStatus`): filled, canceled, expired,
rejected, replaced, suspended, stopped. Everything else keeps polling.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable

from trading_platform.persistence.db import DbConnection

from storage.fill_repo import upsert_fill
from storage.order_repo import update_status

log = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({
    "filled", "canceled", "cancelled", "expired", "rejected",
    "replaced", "suspended", "stopped", "done_for_day",
})


def _client():
    from alpaca.trading.client import TradingClient

    key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_API_SECRET"]
    base = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    return TradingClient(key, secret, paper="paper" in base)


def _open_orders(db: DbConnection) -> list[dict[str, Any]]:
    rows = db.execute(
        """
        SELECT id, client_order_id, broker_order_id, status
        FROM orders
        WHERE status NOT IN ({})
        """.format(",".join("?" * len(TERMINAL_STATUSES))),
        tuple(TERMINAL_STATUSES),
    ).fetchall()
    return [dict(r) for r in rows]


def poll_fills(db: DbConnection, *, client=None, alert_hook=None) -> int:
    """Poll Alpaca for every non-terminal order and upsert fills. Returns
    the number of fill rows inserted this pass."""
    open_orders = _open_orders(db)
    if not open_orders:
        return 0

    c = client if client is not None else _client()
    inserted = 0
    for row in open_orders:
        coi = row["client_order_id"]
        try:
            resp = c.get_order_by_client_id(coi)
        except Exception as exc:  # noqa: BLE001
            log.warning("fill poll failed for %s: %s", coi, exc)
            continue

        status = str(getattr(resp, "status", "") or "").lower()
        filled_qty = int(float(getattr(resp, "filled_qty", 0) or 0))
        filled_avg_price = getattr(resp, "filled_avg_price", None)
        try:
            filled_avg_price = float(filled_avg_price) if filled_avg_price else None
        except (TypeError, ValueError):
            filled_avg_price = None
        broker_id = str(getattr(resp, "id", "") or "") or row.get("broker_order_id")

        fill_id = upsert_fill(
            db,
            order_id=row["id"],
            client_order_id=coi,
            filled_qty=filled_qty,
            filled_avg_price=filled_avg_price,
            status=status,
        )
        # If upsert_fill returned an existing id, the (status, qty) pair
        # was unchanged — no-op. Count actual inserts only.
        prior_latest = db.execute(
            "SELECT COUNT(*) AS n FROM fills WHERE client_order_id = ? AND id = ?",
            (coi, fill_id),
        ).fetchone()
        if prior_latest and prior_latest["n"]:
            # We can't cheaply tell from here whether the row is new vs dedup,
            # so assume inserted if status differs from the order's last known.
            if row["status"] != status:
                inserted += 1
                if alert_hook is not None and status == "filled":
                    try:
                        alert_hook(
                            "fill",
                            {
                                "client_order_id": coi,
                                "filled_qty": filled_qty,
                                "filled_avg_price": filled_avg_price,
                            },
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("alert hook raised on fill")

        update_status(db, client_order_id=coi, status=status, broker_order_id=broker_id)

    return inserted
