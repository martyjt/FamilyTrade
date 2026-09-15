"""Pure target geometry and deterministic setup tie-breaking."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from familytrade.simulation.orders import tick_round
from familytrade.simulation.state import SetupSnapshot


@dataclass(frozen=True, slots=True)
class BreakoutGeometry:
    threshold: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal


def breakout_geometry(
    *,
    side: str,
    zone_low: Decimal,
    zone_high: Decimal,
    close: Decimal,
    vwap: Decimal,
    unit: Decimal,
    break_multiple: Decimal,
    pullback_multiple: Decimal,
    stop_multiple: Decimal,
    measured_move_multiple: Decimal,
) -> BreakoutGeometry:
    """Freeze the reviewed breakout threshold, pullback, stop and measured move."""
    if side == "long":
        threshold = zone_high + break_multiple * unit
        entry = zone_high + pullback_multiple * unit
        stop = min(zone_low - stop_multiple * unit, vwap - stop_multiple * unit)
        target = zone_high + (close - zone_high) * measured_move_multiple
    else:
        threshold = zone_low - break_multiple * unit
        entry = zone_low - pullback_multiple * unit
        stop = max(zone_high + stop_multiple * unit, vwap + stop_multiple * unit)
        target = zone_low - (zone_low - close) * measured_move_multiple
    return BreakoutGeometry(threshold=threshold, entry=entry, stop=stop, target=target)


def breakout_confirmed(
    *, side: str, previous_close: Decimal, close: Decimal, threshold: Decimal, mode: str
) -> bool:
    beyond = close > threshold if side == "long" else close < threshold
    if mode == "beyond":
        return beyond
    crossed = previous_close <= threshold if side == "long" else previous_close >= threshold
    return beyond and crossed


def retested(*, side: str, low: Decimal, high: Decimal, entry: Decimal) -> bool:
    return low <= entry if side == "long" else high >= entry


def directional_approach(
    *, side: str, previous_close: Decimal, close: Decimal, zone_low: Decimal, zone_high: Decimal
) -> bool:
    boundary = zone_high if side == "long" else zone_low
    return abs(close - boundary) < abs(previous_close - boundary)


def breakout_is_expired(*, arm_index: int, current_index: int, expiry_bars: int) -> bool:
    return current_index - arm_index > expiry_bars


def cooldown_allows(*, last_fill_index: int | None, current_index: int, cooldown_bars: int) -> bool:
    return last_fill_index is None or current_index - last_fill_index > cooldown_bars


def recent_peak_stop(
    *, side: str, prices: tuple[Decimal, ...], zone_boundary: Decimal, buffer: Decimal
) -> Decimal:
    if not prices:
        raise ValueError("a complete recent-peak window is required")
    if side == "long":
        return min(zone_boundary - buffer, min(prices) - buffer)
    return max(zone_boundary + buffer, max(prices) + buffer)


def r_multiple_target(
    *, side: str, entry: Decimal, stop: Decimal, multiple: Decimal, tick: Decimal
) -> Decimal:
    direction = Decimal(1) if side == "long" else Decimal(-1)
    raw = entry + direction * abs(entry - stop) * multiple
    return tick_round(raw, tick, role="target", side="sell" if side == "long" else "buy")


def valid_bracket(*, side: str, entry: Decimal, stop: Decimal, target: Decimal) -> bool:
    return stop < entry < target if side == "long" else target < entry < stop


def select_candidate(candidates: tuple[SetupSnapshot, ...]) -> SetupSnapshot | None:
    """Breakout wins same-side ties; remaining ties are stable by setup identity."""
    if not candidates:
        return None
    sides = {candidate.side for candidate in candidates}
    if len(sides) != 1:
        return None

    def normalized_distance(item: SetupSnapshot) -> Decimal:
        close = next(
            (
                value.value
                for value in item.frozen_feature_values
                if value.feature_id == "__ft07_exec_close" and isinstance(value.value, Decimal)
            ),
            item.entry,
        )
        unit = Decimal(item.unit)
        return abs(close - item.entry) / unit if unit > 0 else Decimal("Infinity")

    return min(
        candidates,
        key=lambda item: (
            0 if item.family == "breakout" else 1,
            normalized_distance(item),
            -item.setup_sequence,
            item.setup_id,
        ),
    )
