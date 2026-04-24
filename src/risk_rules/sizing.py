"""Position sizing — Decimal only.

Formula (ARCHITECTURE §3.3 / EXECUTION_PLAN §Phase 3):

    qty = floor( min(equity * MAX_PER_SYMBOL_PCT,
                     equity * MAX_TOTAL_PCT - current_exposure) / price )

If the result is ≤ 0 (symbol cap already breached, or total exposure cap
would be exceeded), returns 0. Zero qty == "do not trade", handled by the
caller as a reject reason.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, InvalidOperation

# Defaults per ARCHITECTURE §3.3.
MAX_PER_SYMBOL_PCT = Decimal("0.03")
MAX_TOTAL_EXPOSURE_PCT = Decimal("0.50")  # paper phase


def _to_decimal(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"cannot coerce {x!r} to Decimal") from exc


def size_order(
    equity,
    current_exposure,
    price,
    *,
    max_per_symbol_pct: Decimal = MAX_PER_SYMBOL_PCT,
    max_total_pct: Decimal = MAX_TOTAL_EXPOSURE_PCT,
) -> int:
    """Return the integer share count to order. Never negative.

    All inputs coerced to Decimal — floats accepted at the boundary but
    never used for the actual math (CLAUDE.md: Decimal, never float).
    """
    eq = _to_decimal(equity)
    exp = _to_decimal(current_exposure)
    px = _to_decimal(price)
    if px <= 0 or eq <= 0:
        return 0

    per_symbol_budget = eq * max_per_symbol_pct
    remaining_total = eq * max_total_pct - exp
    budget = min(per_symbol_budget, remaining_total)
    if budget <= 0:
        return 0

    qty = (budget / px).to_integral_value(rounding=ROUND_DOWN)
    return max(int(qty), 0)
