"""Portfolio snapshot — Alpaca account equity + open positions.

Fetched on every risk evaluation (ARCHITECTURE §3.3). This keeps us honest
about current exposure; the alternative (caching) is a footgun.

Decimal end-to-end — Alpaca returns strings, we never let them touch float.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PositionInfo:
    symbol: str
    qty: Decimal
    market_value: Decimal
    avg_entry_price: Decimal


@dataclass(frozen=True)
class Portfolio:
    equity: Decimal
    cash: Decimal
    positions: dict[str, PositionInfo] = field(default_factory=dict)

    @property
    def total_exposure(self) -> Decimal:
        return sum((p.market_value for p in self.positions.values()), start=Decimal("0"))

    @property
    def open_position_count(self) -> int:
        return sum(1 for p in self.positions.values() if p.qty != 0)

    def exposure_for(self, symbol: str) -> Decimal:
        p = self.positions.get(symbol.upper())
        return p.market_value if p else Decimal("0")


def _client():
    from alpaca.trading.client import TradingClient

    key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_API_SECRET"]
    base = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    return TradingClient(key, secret, paper="paper" in base)


def fetch_portfolio(client=None) -> Portfolio:
    c = client if client is not None else _client()
    acct = c.get_account()
    equity = Decimal(str(acct.equity))
    cash = Decimal(str(acct.cash))
    raw_positions = c.get_all_positions()
    positions: dict[str, PositionInfo] = {}
    for p in raw_positions:
        sym = str(p.symbol).upper()
        positions[sym] = PositionInfo(
            symbol=sym,
            qty=Decimal(str(p.qty)),
            market_value=Decimal(str(p.market_value)),
            avg_entry_price=Decimal(str(p.avg_entry_price)),
        )
    return Portfolio(equity=equity, cash=cash, positions=positions)
