"""Alpaca websocket wrapper. Subscribes to quotes + minute bars for the
watchlist and invokes a handler for each message.

Kept deliberately thin — the Data Agent owns state, indicator recomputation,
cache writes, and retention. This module's job is: "here's a new quote/bar
for SYM, route it to your handler."

Reconnect + backoff is handled by alpaca-py's `StockDataStream` internally.
Stale-data handling lives in the Data Agent (freshness filter on read).
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuoteMsg:
    symbol: str
    ts: datetime
    ask_price: float
    bid_price: float

    @property
    def mid(self) -> float:
        return (self.ask_price + self.bid_price) / 2.0


@dataclass(frozen=True)
class BarMsg:
    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


QuoteHandler = Callable[[QuoteMsg], Awaitable[None]]
BarHandler = Callable[[BarMsg], Awaitable[None]]


def _coerce_ts(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    # alpaca-py returns pandas.Timestamp in some paths; .to_pydatetime() works.
    if hasattr(ts, "to_pydatetime"):
        dt = ts.to_pydatetime()
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(ts))


class AlpacaQuoteStream:
    """Minimal async websocket runner.

    Usage:
        stream = AlpacaQuoteStream(symbols=("AAPL", "MSFT"))
        await stream.run(on_quote=handle_quote, on_bar=handle_bar)

    Cancel the awaiting task (or call `stop()`) to shut down.
    """

    def __init__(
        self,
        symbols: Iterable[str],
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        feed: str = "iex",  # free tier
    ) -> None:
        self._symbols = tuple(s.upper() for s in symbols)
        self._api_key = api_key or os.environ["ALPACA_API_KEY"]
        self._secret_key = secret_key or os.environ["ALPACA_API_SECRET"]
        self._feed = feed
        self._stream = None

    async def run(
        self,
        *,
        on_quote: QuoteHandler | None = None,
        on_bar: BarHandler | None = None,
    ) -> None:
        from alpaca.data.live import StockDataStream

        self._stream = StockDataStream(
            api_key=self._api_key,
            secret_key=self._secret_key,
            feed=self._feed,
        )

        if on_quote is not None:

            async def _quote_handler(msg) -> None:
                q = QuoteMsg(
                    symbol=str(msg.symbol).upper(),
                    ts=_coerce_ts(msg.timestamp),
                    ask_price=float(msg.ask_price),
                    bid_price=float(msg.bid_price),
                )
                try:
                    await on_quote(q)
                except Exception:  # noqa: BLE001
                    log.exception("quote handler raised")

            self._stream.subscribe_quotes(_quote_handler, *self._symbols)

        if on_bar is not None:

            async def _bar_handler(msg) -> None:
                b = BarMsg(
                    symbol=str(msg.symbol).upper(),
                    ts=_coerce_ts(msg.timestamp),
                    open=float(msg.open),
                    high=float(msg.high),
                    low=float(msg.low),
                    close=float(msg.close),
                    volume=int(msg.volume),
                )
                try:
                    await on_bar(b)
                except Exception:  # noqa: BLE001
                    log.exception("bar handler raised")

            self._stream.subscribe_bars(_bar_handler, *self._symbols)

        log.info("alpaca ws starting for %d symbols on %s feed", len(self._symbols), self._feed)
        # _run_forever blocks until the stream is closed or cancelled
        await asyncio.get_event_loop().run_in_executor(None, self._stream.run)

    async def stop(self) -> None:
        if self._stream is not None:
            await self._stream.close()
