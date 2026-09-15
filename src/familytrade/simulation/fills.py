"""Symmetric pure paper-fill selection for one complete fill bar."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class BarPrices:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True, slots=True)
class ProspectiveFill:
    base_price: Decimal
    fill_price: Decimal
    reason: str

    @property
    def slippage(self) -> Decimal:
        return abs(self.fill_price - self.base_price)


def protective_fill(
    *,
    position_side: str,
    stop: Decimal,
    target: Decimal,
    bar: BarPrices,
    tick: Decimal,
    stop_slippage_ticks: int,
    both_hit_policy: str,
) -> ProspectiveFill | None:
    slip = tick * stop_slippage_ticks
    if position_side == "long":
        if bar.open <= stop:
            return ProspectiveFill(bar.open, bar.open - slip, "STOP_GAP")
        if bar.open >= target:
            return ProspectiveFill(bar.open, bar.open, "TARGET_GAP")
        stop_hit, target_hit = bar.low <= stop, bar.high >= target
        if stop_hit and target_hit:
            return ProspectiveFill(
                stop if both_hit_policy == "stop_first" else target,
                stop - slip if both_hit_policy == "stop_first" else target,
                "BOTH_HIT_STOP_FIRST"
                if both_hit_policy == "stop_first"
                else "BOTH_HIT_TARGET_FIRST",
            )
        if stop_hit:
            return ProspectiveFill(stop, stop - slip, "STOP_TOUCH")
        if target_hit:
            return ProspectiveFill(target, target, "TARGET_TOUCH")
    else:
        if bar.open >= stop:
            return ProspectiveFill(bar.open, bar.open + slip, "STOP_GAP")
        if bar.open <= target:
            return ProspectiveFill(bar.open, bar.open, "TARGET_GAP")
        stop_hit, target_hit = bar.high >= stop, bar.low <= target
        if stop_hit and target_hit:
            return ProspectiveFill(
                stop if both_hit_policy == "stop_first" else target,
                stop + slip if both_hit_policy == "stop_first" else target,
                "BOTH_HIT_STOP_FIRST"
                if both_hit_policy == "stop_first"
                else "BOTH_HIT_TARGET_FIRST",
            )
        if stop_hit:
            return ProspectiveFill(stop, stop + slip, "STOP_TOUCH")
        if target_hit:
            return ProspectiveFill(target, target, "TARGET_TOUCH")
    return None
