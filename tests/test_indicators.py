"""Phase 1 indicators — synthetic OHLCV → asserted values within 0.1."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pandas_ta_classic as pta


def _synthetic_bars(n: int = 300, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    # Geometric-ish walk so we end up with a non-degenerate RSI/SMA/ATR.
    returns = rng.normal(loc=0.0003, scale=0.012, size=n)
    close = 100.0 * np.exp(np.cumsum(returns))
    high = close * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, n)))
    open_ = close * (1 + rng.normal(0, 0.002, n))
    volume = rng.integers(1_000_000, 10_000_000, n).astype(float)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )


def test_rsi14_within_tolerance() -> None:
    from market.indicators import compute_features

    bars = _synthetic_bars()
    feats = compute_features(bars)
    expected_rsi = float(pta.rsi(bars["close"], length=14).iloc[-1])
    assert not math.isnan(feats.rsi14)
    assert abs(feats.rsi14 - expected_rsi) < 0.1


def test_smas_match_pandas_ta() -> None:
    from market.indicators import compute_features

    bars = _synthetic_bars()
    feats = compute_features(bars)
    for length, got in [(20, feats.sma20), (50, feats.sma50), (200, feats.sma200)]:
        expected = float(pta.sma(bars["close"], length=length).iloc[-1])
        assert abs(got - expected) < 0.1, f"SMA-{length} off: {got} vs {expected}"


def test_atr14_positive_and_finite() -> None:
    from market.indicators import compute_features

    bars = _synthetic_bars()
    feats = compute_features(bars)
    assert not math.isnan(feats.atr14)
    assert feats.atr14 > 0


def test_short_history_returns_nan_for_long_windows() -> None:
    from market.indicators import compute_features

    bars = _synthetic_bars(n=40)  # too short for SMA-50, SMA-200
    feats = compute_features(bars)
    assert not math.isnan(feats.sma20)
    assert math.isnan(feats.sma50)
    assert math.isnan(feats.sma200)


def test_price_override() -> None:
    from market.indicators import compute_features

    bars = _synthetic_bars()
    latest_price = 999.99
    feats = compute_features(bars, latest_price=latest_price)
    assert feats.price == latest_price
    assert math.isclose(
        feats.price_vs_sma50_pct, (latest_price - feats.sma50) / feats.sma50 * 100.0, rel_tol=1e-9
    )


def test_missing_columns_raises() -> None:
    import pytest

    from market.indicators import compute_features

    with pytest.raises(ValueError, match="missing required columns"):
        compute_features(pd.DataFrame({"close": [1.0, 2.0]}))
