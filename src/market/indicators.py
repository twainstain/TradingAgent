"""Indicator computations from daily OHLCV bars.

Deliberately pure functions over pandas DataFrames — no I/O, no state. The
data agent feeds in a `bars_daily` slice per symbol and receives the features
the Strategy Agent needs.

Phase 1 feature set (ARCHITECTURE §3.1):
  - RSI-14
  - SMA-20, SMA-50, SMA-200
  - 20-day average volume
  - ATR-14
  - price vs SMA-50 (%)

We return NaN (not zero, not None) for values that can't be computed from
a short history — the data agent filters those at write-time or the read-time
freshness filter catches partial snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
import pandas_ta_classic as pta  # pandas-ta was delisted from PyPI; this is the drop-in fork.


@dataclass(frozen=True)
class Features:
    price: float
    rsi14: float
    sma20: float
    sma50: float
    sma200: float
    avg_vol_20: float
    atr14: float
    price_vs_sma50_pct: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "price": self.price,
            "rsi14": self.rsi14,
            "sma20": self.sma20,
            "sma50": self.sma50,
            "sma200": self.sma200,
            "avg_vol_20": self.avg_vol_20,
            "atr14": self.atr14,
            "price_vs_sma50_pct": self.price_vs_sma50_pct,
        }


def _last(series: pd.Series) -> float:
    if series is None or len(series) == 0:
        return float("nan")
    val = series.iloc[-1]
    return float(val) if pd.notna(val) else float("nan")


def compute_features(bars: pd.DataFrame, latest_price: float | None = None) -> Features:
    """Compute indicator features from a daily OHLCV DataFrame.

    Args:
        bars: DataFrame with columns ``open, high, low, close, volume``, sorted
              ascending by date. Should contain ≥200 rows for all features to
              be non-NaN; shorter inputs return NaN for the longer-window
              indicators.
        latest_price: Optional override for the "current" price (e.g. the latest
              intraday quote from the websocket). If None, uses the final close
              of `bars`.

    Returns:
        Features dataclass. Any un-computable value is NaN.
    """
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars is missing required columns: {missing}")

    close = bars["close"].astype(float)
    volume = bars["volume"].astype(float)

    price = float(latest_price) if latest_price is not None else _last(close)

    rsi14 = _last(pta.rsi(close, length=14))
    sma20 = _last(pta.sma(close, length=20))
    sma50 = _last(pta.sma(close, length=50))
    sma200 = _last(pta.sma(close, length=200))
    avg_vol_20 = _last(volume.rolling(20).mean())
    atr14 = _last(pta.atr(bars["high"].astype(float), bars["low"].astype(float), close, length=14))

    if pd.notna(sma50) and sma50 != 0 and pd.notna(price):
        price_vs_sma50_pct = (price - sma50) / sma50 * 100.0
    else:
        price_vs_sma50_pct = float("nan")

    return Features(
        price=price,
        rsi14=rsi14,
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        avg_vol_20=avg_vol_20,
        atr14=atr14,
        price_vs_sma50_pct=price_vs_sma50_pct,
    )
