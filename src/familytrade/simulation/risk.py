"""Exact USD accounting, sizing, marking, and latch calculations."""

from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal, localcontext

_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN, Emin=-6143, Emax=6144)
_CENT = Decimal("0.01")


def money_round(value: Decimal) -> Decimal:
    with localcontext(_CONTEXT):
        return value.quantize(_CENT, rounding=ROUND_HALF_EVEN)


def commission(rate: Decimal, quantity: int) -> Decimal:
    return money_round(rate * quantity)


def price_pnl(
    *, side: str, entry: Decimal, exit_or_mark: Decimal, multiplier: Decimal, quantity: int
) -> Decimal:
    direction = Decimal(1) if side == "long" else Decimal(-1)
    return money_round(direction * (exit_or_mark - entry) * multiplier * quantity)


def modeled_total_loss(
    *,
    entry: Decimal,
    slipped_stop: Decimal,
    multiplier: Decimal,
    quantity: int,
    commission_rate: Decimal,
) -> Decimal:
    return (
        abs(entry - slipped_stop) * multiplier * quantity
        + commission(commission_rate, quantity)
        + commission(commission_rate, quantity)
    )


def stop_fraction_quantity(
    *,
    starting_cash: Decimal,
    current_equity: Decimal,
    fraction: Decimal,
    max_quantity: int,
    per_entry_cap: Decimal,
    entry: Decimal,
    slipped_stop: Decimal,
    multiplier: Decimal,
    commission_rate: Decimal,
) -> int:
    budget = fraction * min(starting_cash, current_equity)
    one = modeled_total_loss(
        entry=entry,
        slipped_stop=slipped_stop,
        multiplier=multiplier,
        quantity=1,
        commission_rate=commission_rate,
    )
    if one <= 0:
        return 0
    q = min(max_quantity, int((budget / one).to_integral_value(rounding=ROUND_FLOOR)))
    cap = min(budget, per_entry_cap)
    while (
        q
        and modeled_total_loss(
            entry=entry,
            slipped_stop=slipped_stop,
            multiplier=multiplier,
            quantity=q,
            commission_rate=commission_rate,
        )
        > cap
    ):
        q -= 1
    return q
