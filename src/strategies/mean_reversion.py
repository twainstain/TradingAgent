"""Mean-reversion rule (EXECUTION_PLAN §Phase 2).

Condition: `rsi14 < 30 AND volume_today > 1.5 * avg_vol_20 AND price > sma200`
  → emit `Signal(symbol, "buy", …)`

Any missing/NaN input → no signal (no exception).
"""
from __future__ import annotations

from dataclasses import dataclass

from core.signal import Signal
from strategies.base import StrategyContext, _is_num

NAME = "mean_reversion"


@dataclass(frozen=True)
class MeanReversion:
    rsi_threshold: float = 30.0
    volume_multiple: float = 1.5

    @property
    def name(self) -> str:
        return NAME

    def evaluate(self, ctx: StrategyContext) -> Signal | None:
        s = ctx.snapshot
        for x in (s.price, s.rsi14, s.sma200, s.avg_vol_20, ctx.volume_today):
            if not _is_num(x):
                return None

        price = float(s.price)
        rsi = float(s.rsi14)  # type: ignore[arg-type]
        sma200 = float(s.sma200)  # type: ignore[arg-type]
        avg_vol = float(s.avg_vol_20)  # type: ignore[arg-type]
        vol_today = float(ctx.volume_today)  # type: ignore[arg-type]

        if not (rsi < self.rsi_threshold and vol_today > self.volume_multiple * avg_vol and price > sma200):
            return None

        reason = (
            f"rsi14={rsi:.2f}<{self.rsi_threshold} AND "
            f"vol_today={vol_today:.0f}>{self.volume_multiple}*avg_vol_20={avg_vol:.0f} AND "
            f"price={price:.2f}>sma200={sma200:.2f}"
        )
        return Signal(
            symbol=s.symbol,
            side="buy",
            strategy=NAME,
            confidence=1.0,
            reason=reason,
        )
