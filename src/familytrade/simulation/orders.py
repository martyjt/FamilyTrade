"""Pure order price, activation, and expiry helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal


def tick_round(raw: Decimal, tick: Decimal, *, role: str, side: str) -> Decimal:
    """Apply the frozen role/side tick-rounding table exactly once."""
    if not raw.is_finite() or not tick.is_finite() or tick <= 0:
        raise ValueError("finite positive tick geometry is required")
    floors = {("entry", "buy"), ("stop", "buy"), ("target", "sell")}
    mode = ROUND_FLOOR if (role, side) in floors else ROUND_CEILING
    return (raw / tick).to_integral_value(rounding=mode) * tick


def entry_expiry(
    execution_bar_end: datetime,
    submitted_at: datetime,
    execution_interval_seconds: int,
    ttl_execution_bars: int,
) -> datetime:
    elapsed = (submitted_at - execution_bar_end).total_seconds()
    if elapsed < 0:
        raise ValueError("submission precedes decision")
    period = int(elapsed // execution_interval_seconds)
    return execution_bar_end + timedelta(
        seconds=(period + ttl_execution_bars) * execution_interval_seconds
    )


def market_fill_price(open_price: Decimal, tick: Decimal, ticks: int, side: str) -> Decimal:
    return open_price + tick * ticks if side == "buy" else open_price - tick * ticks


def limit_fill(
    *,
    side: str,
    limit: Decimal,
    open_price: Decimal,
    high: Decimal,
    low: Decimal,
    tick: Decimal,
    slippage_ticks: int,
) -> tuple[Decimal, Decimal, str] | None:
    slippage = tick * slippage_ticks
    if side == "buy":
        if open_price <= limit:
            return open_price, min(limit, open_price + slippage), "LIMIT_ENTRY_GAP"
        if low <= limit:
            return limit, limit, "LIMIT_ENTRY_TOUCH"
    else:
        if open_price >= limit:
            return open_price, max(limit, open_price - slippage), "LIMIT_ENTRY_GAP"
        if high >= limit:
            return limit, limit, "LIMIT_ENTRY_TOUCH"
    return None
