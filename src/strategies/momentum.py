"""Momentum rule (EXECUTION_PLAN §Phase 2).

Condition: `price > sma50 > sma200 AND 50 <= rsi14 <= 70 AND close > yesterday_close`
  → emit `Signal(symbol, "buy", …)`

Any missing/NaN input → no signal (no exception).
"""
from __future__ import annotations

from dataclasses import dataclass

from core.signal import Signal
from strategies.base import StrategyContext, _is_num

NAME = "momentum"


@dataclass(frozen=True)
class Momentum:
    rsi_low: float = 50.0
    rsi_high: float = 70.0

    @property
    def name(self) -> str:
        return NAME

    def evaluate(self, ctx: StrategyContext) -> Signal | None:
        s = ctx.snapshot
        for x in (s.price, s.rsi14, s.sma50, s.sma200, ctx.yesterday_close):
            if not _is_num(x):
                return None

        price = float(s.price)
        rsi = float(s.rsi14)  # type: ignore[arg-type]
        sma50 = float(s.sma50)  # type: ignore[arg-type]
        sma200 = float(s.sma200)  # type: ignore[arg-type]
        y_close = float(ctx.yesterday_close)  # type: ignore[arg-type]

        trending_up = price > sma50 > sma200
        rsi_in_band = self.rsi_low <= rsi <= self.rsi_high
        breaking_out = price > y_close

        if not (trending_up and rsi_in_band and breaking_out):
            return None

        reason = (
            f"price={price:.2f}>sma50={sma50:.2f}>sma200={sma200:.2f} AND "
            f"{self.rsi_low}<=rsi14={rsi:.2f}<={self.rsi_high} AND "
            f"price>yesterday_close={y_close:.2f}"
        )
        return Signal(
            symbol=s.symbol,
            side="buy",
            strategy=NAME,
            confidence=1.0,
            reason=reason,
        )
