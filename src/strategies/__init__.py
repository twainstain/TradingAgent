"""Strategy registry + loader.

`config/strategies.yaml` is the single source for which strategies run and
their parameters. `load_enabled_strategies()` returns instances ready to
evaluate.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from strategies.base import Strategy, StrategyContext  # re-exported
from strategies.mean_reversion import NAME as MEAN_REVERSION_NAME
from strategies.mean_reversion import MeanReversion
from strategies.momentum import NAME as MOMENTUM_NAME
from strategies.momentum import Momentum

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "strategies.yaml"

# Name → constructor mapping. Extend here when a new strategy lands (Phase 7).
_REGISTRY: dict[str, Any] = {
    MEAN_REVERSION_NAME: MeanReversion,
    MOMENTUM_NAME: Momentum,
}


def _build(name: str, cfg: dict[str, Any]) -> Strategy:
    cls = _REGISTRY[name]
    # Strip execution-only keys (consumed in Phase 4) before passing to ctor.
    kwargs = {k: v for k, v in cfg.items() if k not in {"enabled", "stop_pct", "take_profit_pct"}}
    return cls(**kwargs)


@lru_cache(maxsize=4)
def load_strategies_config(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    p = Path(path) if path else _DEFAULT_PATH
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cfg = raw.get("strategies") or {}
    if not isinstance(cfg, dict):
        raise ValueError(f"strategies.yaml must have a dict 'strategies' key, got {raw!r}")
    return cfg


def load_enabled_strategies(path: str | Path | None = None) -> list[Strategy]:
    cfg = load_strategies_config(path)
    out: list[Strategy] = []
    for name, params in cfg.items():
        if not isinstance(params, dict) or not params.get("enabled"):
            continue
        if name not in _REGISTRY:
            raise ValueError(f"unknown strategy in config: {name!r}")
        out.append(_build(name, params))
    return out


# ----- Phase 4: execution overrides from the same config -----

DEFAULT_STOP_PCT = -0.02
DEFAULT_TAKE_PROFIT_PCT = 0.04


def execution_overrides(
    strategy_name: str, path: str | Path | None = None
) -> tuple[float, float]:
    """Return (stop_pct, take_profit_pct) for `strategy_name`.

    Falls back to the -2% / +4% defaults (ARCHITECTURE §3.4) if the
    strategy is missing or the config key is absent.
    """
    cfg = load_strategies_config(path)
    params = cfg.get(strategy_name) or {}
    stop = float(params.get("stop_pct", DEFAULT_STOP_PCT))
    tp = float(params.get("take_profit_pct", DEFAULT_TAKE_PROFIT_PCT))
    return stop, tp


__all__ = [
    "Strategy",
    "StrategyContext",
    "load_enabled_strategies",
    "load_strategies_config",
    "execution_overrides",
    "DEFAULT_STOP_PCT",
    "DEFAULT_TAKE_PROFIT_PCT",
]
