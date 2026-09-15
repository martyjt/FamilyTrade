"""Pure decimal feature calculations for the frozen feature-catalogue-v1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from typing import Any, ClassVar

from familytrade.market_data.models import BarSelection
from familytrade.simulation.state import (
    AccumulatorPoint,
    AtrWilderAccumulator,
    DependencyAccumulator,
    EmaAccumulator,
    FeatureRuntimeState,
    FeatureValue,
    NoneAccumulator,
    PivotAccumulator,
    RelativeVolumeAccumulator,
    RollingWindowAccumulator,
    RsiWilderAccumulator,
    SessionLevelAccumulator,
    SessionVwapAccumulator,
    SourceDatasetProvenance,
)
from familytrade.strategies.definitions import FeatureInstance

_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN, Emin=-6143, Emax=6144)
_QUANTUM = Decimal("0.000000000001")


@dataclass(frozen=True, slots=True)
class FeatureBar:
    start_at: datetime
    end_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_bar_record_ids: tuple[str, ...]
    source_dataset_provenance: tuple[SourceDatasetProvenance, ...]
    known_at: datetime
    source_availability_at: tuple[datetime, ...] = ()
    selections: tuple[BarSelection, ...] = ()


def quantize_feature(value: Decimal) -> Decimal:
    """Quantize one formula step under the contract-local decimal128-like context."""
    with localcontext(_CONTEXT):
        if not value.is_finite():
            raise InvalidOperation
        return value.quantize(_QUANTUM)


def bar_input(bar: FeatureBar, name: str) -> Decimal:
    with localcontext(_CONTEXT):
        values = {
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "hl2": (bar.high + bar.low) / Decimal(2),
            "typical": (bar.high + bar.low + bar.close) / Decimal(3),
        }
        return quantize_feature(values[name])


def confirmed_pivot_index(
    values: tuple[Decimal, ...], *, candidate_index: int, left: int, right: int, high: bool
) -> int | None:
    """Return a causally confirmable pivot index using the frozen plateau comparisons."""
    if candidate_index < left or candidate_index + right >= len(values):
        return None
    candidate = values[candidate_index]
    left_values = values[candidate_index - left : candidate_index]
    right_values = values[candidate_index + 1 : candidate_index + right + 1]
    if high:
        confirmed = all(candidate >= item for item in left_values) and all(
            candidate > item for item in right_values
        )
    else:
        confirmed = all(candidate <= item for item in left_values) and all(
            candidate < item for item in right_values
        )
    return candidate_index if confirmed else None


def swing_regime(
    *, prior_high: Decimal, latest_high: Decimal, prior_low: Decimal, latest_low: Decimal
) -> str:
    """Classify four already-confirmed pivots without treating equality as evidence."""
    if latest_high > prior_high and latest_low > prior_low:
        return "bullish"
    if latest_high < prior_high and latest_low < prior_low:
        return "bearish"
    if latest_high == prior_high or latest_low == prior_low:
        return "unknown"
    return "sideways"


def initial_accumulator(feature: FeatureInstance) -> Any:
    name = feature.name
    if name in {"open", "high", "low", "close", "hl2", "typical", "volume"}:
        return NoneAccumulator(kind="none_v1")
    if name in {"sma_v1", "rolling_high_v1", "rolling_low_v1"}:
        return RollingWindowAccumulator(kind="rolling_window_v1", points=(), sum=Decimal(0))
    if name == "ema_v1":
        return EmaAccumulator(kind="ema_v1", seed_points=(), seed_sum=Decimal(0), ema=None)
    if name == "rsi_wilder_v1":
        return RsiWilderAccumulator(
            kind="rsi_wilder_v1",
            previous_close=None,
            delta_count=0,
            seed_gain_sum=Decimal(0),
            seed_loss_sum=Decimal(0),
            average_gain=None,
            average_loss=None,
        )
    if name == "atr_wilder_v1":
        return AtrWilderAccumulator(
            kind="atr_wilder_v1",
            previous_close=None,
            true_range_count=0,
            seed_true_range_sum=Decimal(0),
            average_true_range=None,
        )
    if name == "relative_volume_v1":
        return RelativeVolumeAccumulator(
            kind="relative_volume_v1", prior_volumes=(), prior_volume_sum=Decimal(0)
        )
    if name == "session_vwap_v1":
        return SessionVwapAccumulator(
            kind="session_vwap_v1",
            trading_day=None,
            typical_volume_numerator=Decimal(0),
            volume_denominator=Decimal(0),
            source_bar_record_ids=(),
            source_dataset_provenance=(),
        )
    if name in {"confirmed_pivot_v1", "swing_regime_v1"}:
        return PivotAccumulator(
            kind="pivot_v1",
            candidate_bars=(),
            candidate_source_dataset_provenance=(),
            confirmed_highs=(),
            confirmed_lows=(),
        )
    if name in {"prior_session_high_v1", "prior_session_low_v1"}:
        return SessionLevelAccumulator(
            kind="session_level_v1",
            current_trading_day=None,
            current_high=None,
            current_low=None,
            current_complete=False,
            previous_trading_day=None,
            previous_high=None,
            previous_low=None,
            previous_complete=False,
            current_source_bar_record_ids=(),
            current_source_dataset_provenance=(),
            previous_source_bar_record_ids=(),
            previous_source_dataset_provenance=(),
        )
    dependencies: tuple[str, ...] = ()
    if "level_feature_id" in feature.parameters:
        dependencies = (str(feature.parameters["level_feature_id"]),)
    return DependencyAccumulator(
        kind="dependency_v1", dependency_feature_ids=dependencies, previous_values=()
    )


def initial_runtime(feature: FeatureInstance) -> FeatureRuntimeState:
    return FeatureRuntimeState(
        feature_id=feature.feature_id,
        feature_name=feature.name,
        consecutive_bars=0,
        accumulator=initial_accumulator(feature),
        history=(),
        session_trading_day=None,
        continuity_status="clean",
    )


def break_runtime(runtime: FeatureRuntimeState) -> FeatureRuntimeState:
    """Reset gap-sensitive state without carrying a stale value through a data gap."""
    session = runtime.feature_name in {
        "session_vwap_v1",
        "prior_session_high_v1",
        "prior_session_low_v1",
    }
    return runtime.model_copy(
        update={
            "consecutive_bars": 0,
            "accumulator": initial_accumulator_from_runtime(runtime),
            "continuity_status": "broken_until_session" if session else "broken_until_reseed",
        }
    )


def initial_accumulator_from_runtime(runtime: FeatureRuntimeState) -> Any:
    class _Feature:
        name = runtime.feature_name
        parameters: ClassVar[dict[str, object]] = {}

    return initial_accumulator(_Feature())  # type: ignore[arg-type]


def _int_param(feature: FeatureInstance, name: str) -> int:
    value = feature.parameters[name]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(name)
    return value


def _point(value: Decimal, bar: FeatureBar) -> AccumulatorPoint:
    return AccumulatorPoint(
        evaluation_bar_end=bar.end_at,
        known_at=bar.known_at,
        value=value,
        source_bar_record_ids=bar.source_bar_record_ids,
        source_dataset_provenance=bar.source_dataset_provenance,
    )


def _value(
    feature: FeatureInstance,
    bar: FeatureBar,
    value: Decimal | bool | str | None,
    reason: str | None,
    *,
    sources: tuple[str, ...] | None = None,
    provenance: tuple[SourceDatasetProvenance, ...] | None = None,
    known_at: datetime | None = None,
) -> FeatureValue:
    return FeatureValue(
        feature_id=feature.feature_id,
        interval_seconds=feature.interval_seconds,
        evaluation_bar_end=bar.end_at,
        value_type=feature.output_type,
        unit=feature.unit,
        value=value,
        status="UNKNOWN" if value is None else "KNOWN",
        reason_code=reason,
        source_bar_record_ids=bar.source_bar_record_ids if sources is None else sources,
        source_dataset_provenance=(
            bar.source_dataset_provenance if provenance is None else provenance
        ),
        known_at=bar.known_at if known_at is None else known_at,
    )


def _ordered_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _pivot_advance(
    feature: FeatureInstance,
    acc: PivotAccumulator,
    bar: FeatureBar,
) -> tuple[
    PivotAccumulator,
    Decimal | str | None,
    str | None,
    tuple[str, ...],
    tuple[SourceDatasetProvenance, ...],
    datetime,
]:
    left = _int_param(feature, "left")
    right = _int_param(feature, "right")
    components = feature.interval_seconds // 60
    keep = (left + right + 1) * components
    selections = (*acc.candidate_bars, *bar.selections)[-keep:]
    provenance = (*acc.candidate_source_dataset_provenance, *bar.source_dataset_provenance)[-keep:]
    confirmed_highs, confirmed_lows = acc.confirmed_highs, acc.confirmed_lows
    window_size = (left + right + 1) * components
    evidence_ids = tuple(item.bar.bar_record_id for item in selections)
    known = bar.known_at
    if len(selections) == window_size:
        groups = tuple(
            selections[index : index + components] for index in range(0, window_size, components)
        )
        highs = tuple(max(item.bar.high for item in group) for group in groups)
        lows = tuple(min(item.bar.low for item in group) for group in groups)
        candidate = left
        if (
            confirmed_pivot_index(
                highs, candidate_index=candidate, left=left, right=right, high=True
            )
            is not None
        ):
            point = AccumulatorPoint(
                evaluation_bar_end=groups[candidate][-1].bar.end_at,
                known_at=known,
                value=highs[candidate],
                source_bar_record_ids=evidence_ids,
                source_dataset_provenance=provenance,
            )
            confirmed_highs = (*confirmed_highs, point)[-2:]
        if (
            confirmed_pivot_index(
                lows, candidate_index=candidate, left=left, right=right, high=False
            )
            is not None
        ):
            point = AccumulatorPoint(
                evaluation_bar_end=groups[candidate][-1].bar.end_at,
                known_at=known,
                value=lows[candidate],
                source_bar_record_ids=evidence_ids,
                source_dataset_provenance=provenance,
            )
            confirmed_lows = (*confirmed_lows, point)[-2:]
    next_acc = PivotAccumulator(
        kind="pivot_v1",
        candidate_bars=selections,
        candidate_source_dataset_provenance=provenance,
        confirmed_highs=confirmed_highs,
        confirmed_lows=confirmed_lows,
    )
    if feature.name == "confirmed_pivot_v1":
        points = confirmed_highs if feature.parameters["pivot_kind"] == "high" else confirmed_lows
        value: Decimal | str | None = (
            points[-1].value if points and points[-1].known_at == known else None
        )
        return (
            next_acc,
            value,
            None if value is not None else "NOT_READY_PIVOTS",
            evidence_ids,
            provenance,
            known,
        )
    if len(confirmed_highs) < 2 or len(confirmed_lows) < 2:
        return next_acc, None, "NOT_READY_PIVOTS", evidence_ids, provenance, known
    regime = swing_regime(
        prior_high=confirmed_highs[-2].value,
        latest_high=confirmed_highs[-1].value,
        prior_low=confirmed_lows[-2].value,
        latest_low=confirmed_lows[-1].value,
    )
    if regime == "unknown":
        return next_acc, None, "NOT_READY_PIVOTS", evidence_ids, provenance, known
    source_points = (*confirmed_highs[-2:], *confirmed_lows[-2:])
    sources = _ordered_unique(
        tuple(source for point in source_points for source in point.source_bar_record_ids)
    )
    prov_by_id = {
        item.bar_record_id: item
        for point in source_points
        for item in point.source_dataset_provenance
    }
    return (
        next_acc,
        regime,
        None,
        sources,
        tuple(prov_by_id[item] for item in sources),
        max(point.known_at for point in source_points),
    )


def advance_feature(
    feature: FeatureInstance,
    runtime: FeatureRuntimeState,
    bar: FeatureBar,
    *,
    trading_day: date | None,
    dependencies: dict[str, FeatureValue],
    tick_size: Decimal,
) -> tuple[FeatureRuntimeState, FeatureValue]:
    """Advance one feature by one complete, valid bar."""
    name = feature.name
    count = runtime.consecutive_bars + 1
    acc = runtime.accumulator
    result: FeatureValue
    try:
        if name in {"open", "high", "low", "close", "hl2", "typical", "volume"}:
            result = _value(feature, bar, bar_input(bar, name), None)
        elif name in {"sma_v1", "rolling_high_v1", "rolling_low_v1"}:
            assert isinstance(acc, RollingWindowAccumulator)
            n = _int_param(feature, "n")
            input_name = str(feature.parameters.get("input", "high" if "high" in name else "low"))
            current = bar_input(bar, input_name)
            points = acc.points
            if name == "sma_v1":
                points = (*points, _point(current, bar))[-n:]
                total = quantize_feature(sum((p.value for p in points), Decimal(0)))
                value = quantize_feature(total / Decimal(n)) if len(points) == n else None
                reason = None if value is not None else "NOT_READY_WARMUP"
            else:
                eligible = points[-n:]
                value = None
                if len(eligible) == n:
                    values = [p.value for p in eligible]
                    value = max(values) if name == "rolling_high_v1" else min(values)
                reason = None if value is not None else "NOT_READY_LEVEL"
                points = (*points, _point(current, bar))[-n:]
                total = quantize_feature(sum((p.value for p in points), Decimal(0)))
            acc = RollingWindowAccumulator(kind="rolling_window_v1", points=points, sum=total)
            result = _value(feature, bar, value, reason)
        elif name == "ema_v1":
            assert isinstance(acc, EmaAccumulator)
            n = _int_param(feature, "n")
            current = bar_input(bar, str(feature.parameters["input"]))
            if acc.ema is None:
                points = (*acc.seed_points, _point(current, bar))[-n:]
                total = quantize_feature(sum((p.value for p in points), Decimal(0)))
                ema = quantize_feature(total / Decimal(n)) if len(points) == n else None
            else:
                points, total = acc.seed_points, acc.seed_sum
                alpha = Decimal(2) / Decimal(n + 1)
                ema = quantize_feature(alpha * current + (Decimal(1) - alpha) * acc.ema)
            acc = EmaAccumulator(kind="ema_v1", seed_points=points, seed_sum=total, ema=ema)
            result = _value(feature, bar, ema, None if ema is not None else "NOT_READY_WARMUP")
        elif name == "rsi_wilder_v1":
            assert isinstance(acc, RsiWilderAccumulator)
            n = _int_param(feature, "n")
            close = bar_input(bar, "close")
            current_point = _point(close, bar)
            if acc.previous_close is None:
                next_acc = acc.model_copy(update={"previous_close": current_point})
                result = _value(feature, bar, None, "NOT_READY_RSI")
                acc = next_acc
            else:
                delta = close - acc.previous_close.value
                gain, loss = max(delta, Decimal(0)), max(-delta, Decimal(0))
                delta_count = acc.delta_count + 1
                if acc.average_gain is None or acc.average_loss is None:
                    gain_sum = quantize_feature(acc.seed_gain_sum + gain)
                    loss_sum = quantize_feature(acc.seed_loss_sum + loss)
                    avg_gain = quantize_feature(gain_sum / Decimal(n)) if delta_count == n else None
                    avg_loss = quantize_feature(loss_sum / Decimal(n)) if delta_count == n else None
                else:
                    gain_sum, loss_sum = acc.seed_gain_sum, acc.seed_loss_sum
                    avg_gain = quantize_feature((acc.average_gain * (n - 1) + gain) / n)
                    avg_loss = quantize_feature((acc.average_loss * (n - 1) + loss) / n)
                acc = RsiWilderAccumulator(
                    kind="rsi_wilder_v1",
                    previous_close=current_point,
                    delta_count=delta_count,
                    seed_gain_sum=gain_sum,
                    seed_loss_sum=loss_sum,
                    average_gain=avg_gain,
                    average_loss=avg_loss,
                )
                if avg_gain is None or avg_loss is None:
                    rsi = None
                elif avg_gain == 0 and avg_loss == 0:
                    rsi = Decimal(50)
                elif avg_loss == 0:
                    rsi = Decimal(100)
                elif avg_gain == 0:
                    rsi = Decimal(0)
                else:
                    rsi = quantize_feature(Decimal(100) - Decimal(100) / (1 + avg_gain / avg_loss))
                result = _value(feature, bar, rsi, None if rsi is not None else "NOT_READY_RSI")
        elif name == "atr_wilder_v1":
            assert isinstance(acc, AtrWilderAccumulator)
            n = _int_param(feature, "n")
            close_point = _point(bar.close, bar)
            tr = bar.high - bar.low
            if acc.previous_close is not None:
                tr = max(
                    tr,
                    abs(bar.high - acc.previous_close.value),
                    abs(bar.low - acc.previous_close.value),
                )
            tr = quantize_feature(tr)
            tr_count = acc.true_range_count + 1
            if acc.average_true_range is None:
                seed_sum = quantize_feature(acc.seed_true_range_sum + tr)
                average = quantize_feature(seed_sum / Decimal(n)) if tr_count == n else None
            else:
                seed_sum = acc.seed_true_range_sum
                average = quantize_feature((acc.average_true_range * (n - 1) + tr) / n)
            acc = AtrWilderAccumulator(
                kind="atr_wilder_v1",
                previous_close=close_point,
                true_range_count=tr_count,
                seed_true_range_sum=seed_sum,
                average_true_range=average,
            )
            result = _value(feature, bar, average, None if average is not None else "NOT_READY_ATR")
        elif name == "relative_volume_v1":
            assert isinstance(acc, RelativeVolumeAccumulator)
            n = _int_param(feature, "n")
            prior = acc.prior_volumes[-n:]
            if len(prior) < n:
                value, reason = None, "NOT_READY_WARMUP"
            elif acc.prior_volume_sum == 0:
                value, reason = None, "ZERO_DENOMINATOR"
            else:
                value = quantize_feature(bar.volume / (acc.prior_volume_sum / n))
                reason = None
            points = (*prior, _point(bar.volume, bar))[-n:]
            total = quantize_feature(sum((p.value for p in points), Decimal(0)))
            acc = RelativeVolumeAccumulator(
                kind="relative_volume_v1", prior_volumes=points, prior_volume_sum=total
            )
            result = _value(feature, bar, value, reason)
        elif name == "session_vwap_v1":
            assert isinstance(acc, SessionVwapAccumulator)
            if acc.trading_day != trading_day:
                numerator, denominator = Decimal(0), Decimal(0)
                source_ids: tuple[str, ...] = ()
                source_prov: tuple[SourceDatasetProvenance, ...] = ()
            else:
                numerator, denominator = acc.typical_volume_numerator, acc.volume_denominator
                source_ids, source_prov = acc.source_bar_record_ids, acc.source_dataset_provenance
            numerator = quantize_feature(numerator + bar_input(bar, "typical") * bar.volume)
            denominator = quantize_feature(denominator + bar.volume)
            source_ids = _ordered_unique((*source_ids, *bar.source_bar_record_ids))
            prov_by_id = {
                p.bar_record_id: p for p in (*source_prov, *bar.source_dataset_provenance)
            }
            source_prov = tuple(prov_by_id[item] for item in source_ids)
            acc = SessionVwapAccumulator(
                kind="session_vwap_v1",
                trading_day=trading_day,
                typical_volume_numerator=numerator,
                volume_denominator=denominator,
                source_bar_record_ids=source_ids,
                source_dataset_provenance=source_prov,
            )
            value = quantize_feature(numerator / denominator) if denominator else None
            result = _value(
                feature,
                bar,
                value,
                None if value is not None else "ZERO_DENOMINATOR",
                sources=source_ids,
                provenance=source_prov,
            )
        elif name in {"confirmed_pivot_v1", "swing_regime_v1"}:
            assert isinstance(acc, PivotAccumulator)
            acc, pivot_value, reason, sources, provenance, known = _pivot_advance(feature, acc, bar)
            result = _value(
                feature,
                bar,
                pivot_value,
                reason,
                sources=sources,
                provenance=provenance,
                known_at=known,
            )
        elif name in {"prior_session_high_v1", "prior_session_low_v1"}:
            assert isinstance(acc, SessionLevelAccumulator)
            if acc.current_trading_day != trading_day:
                previous_day = acc.current_trading_day
                previous_high = acc.current_high
                previous_low = acc.current_low
                previous_complete = previous_day is not None and acc.current_complete
                previous_ids = acc.current_source_bar_record_ids
                previous_provenance = acc.current_source_dataset_provenance
                current_high, current_low = bar.high, bar.low
                current_ids = bar.source_bar_record_ids
                current_provenance = bar.source_dataset_provenance
            else:
                previous_day = acc.previous_trading_day
                previous_high, previous_low = acc.previous_high, acc.previous_low
                previous_complete = acc.previous_complete
                previous_ids = acc.previous_source_bar_record_ids
                previous_provenance = acc.previous_source_dataset_provenance
                current_high = (
                    bar.high if acc.current_high is None else max(acc.current_high, bar.high)
                )
                current_low = bar.low if acc.current_low is None else min(acc.current_low, bar.low)
                current_ids = _ordered_unique(
                    (*acc.current_source_bar_record_ids, *bar.source_bar_record_ids)
                )
                by_id = {
                    item.bar_record_id: item
                    for item in (
                        *acc.current_source_dataset_provenance,
                        *bar.source_dataset_provenance,
                    )
                }
                current_provenance = tuple(by_id[item] for item in current_ids)
            acc = SessionLevelAccumulator(
                kind="session_level_v1",
                current_trading_day=trading_day,
                current_high=current_high,
                current_low=current_low,
                current_complete=True,
                previous_trading_day=previous_day,
                previous_high=previous_high,
                previous_low=previous_low,
                previous_complete=previous_complete,
                current_source_bar_record_ids=current_ids,
                current_source_dataset_provenance=current_provenance,
                previous_source_bar_record_ids=previous_ids,
                previous_source_dataset_provenance=previous_provenance,
            )
            session_level = previous_high if name == "prior_session_high_v1" else previous_low
            if not previous_complete:
                session_level = None
            result = _value(
                feature,
                bar,
                session_level,
                None if session_level is not None else "NOT_READY_PRIOR_SESSION",
                sources=previous_ids,
                provenance=previous_provenance,
                known_at=bar.start_at,
            )
        elif name in {"level_touch_v1", "level_cross"}:
            assert isinstance(acc, DependencyAccumulator)
            dep_id = str(feature.parameters["level_feature_id"])
            level = dependencies.get(dep_id)
            if level is None or level.status == "UNKNOWN" or not isinstance(level.value, Decimal):
                boolean_value, reason = None, "NOT_READY_LEVEL"
            elif name == "level_touch_v1":
                tolerance = tick_size * _int_param(feature, "tolerance_ticks")
                boolean_value, reason = (
                    bar.high >= level.value - tolerance and bar.low <= level.value + tolerance,
                    None,
                )
            else:
                previous_level = next(
                    (x for x in acc.previous_values if x.feature_id == dep_id), None
                )
                previous_close = dependencies.get("__previous_close__")
                if (
                    previous_level is None
                    or previous_close is None
                    or not isinstance(previous_level.value, Decimal)
                    or not isinstance(previous_close.value, Decimal)
                ):
                    boolean_value, reason = None, "NOT_READY_LEVEL"
                elif str(feature.parameters["direction"]) == "above":
                    boolean_value, reason = (
                        previous_close.value <= previous_level.value and bar.close > level.value,
                        None,
                    )
                else:
                    boolean_value, reason = (
                        previous_close.value >= previous_level.value and bar.close < level.value,
                        None,
                    )
            acc = DependencyAccumulator(
                kind="dependency_v1",
                dependency_feature_ids=(dep_id,),
                previous_values=tuple(x for x in (level,) if x is not None),
            )
            result = _value(feature, bar, boolean_value, reason)
        else:
            result = _value(
                feature,
                bar,
                None,
                "NOT_READY_PIVOTS" if "pivot" in name or "regime" in name else "NOT_READY_SESSION",
            )
    except ArithmeticError, InvalidOperation, KeyError, TypeError, ValueError:
        result = _value(feature, bar, None, "NUMERIC_DOMAIN")
    history = (*runtime.history, result)[-502:]
    return (
        runtime.model_copy(
            update={
                "consecutive_bars": count,
                "accumulator": acc,
                "history": history,
                "session_trading_day": trading_day,
                "continuity_status": "clean",
            }
        ),
        result,
    )
