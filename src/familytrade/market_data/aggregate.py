"""Pure, session-anchored completed-bar aggregation."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from familytrade.market_data.models import (
    AggregatedFullBar,
    AggregateGap,
    AggregateRequest,
    AggregateResult,
    MarketDataCode,
    MarketDataError,
    ScheduledPartialBar,
)


def aggregate_completed_bars(request: AggregateRequest) -> AggregateResult:
    bars = sorted(request.source_bars, key=lambda b: (b.start_at, b.bar_record_id))
    if not bars:
        return AggregateResult(full_bars=(), scheduled_partial_bars=(), rejected_gaps=())
    source_interval = bars[0].interval_seconds
    if (
        source_interval >= request.target_interval_seconds
        or request.target_interval_seconds % source_interval
    ):
        raise MarketDataError(
            MarketDataCode.VALIDATION_ERROR,
            "Source interval must be smaller and divide target interval.",
            422,
        )
    if any(b.interval_seconds != source_interval or b.quality != "valid" for b in bars):
        raise MarketDataError(
            MarketDataCode.VALIDATION_ERROR, "Aggregation requires one valid source interval.", 422
        )
    by_start = {bar.start_at: bar for bar in bars}
    step, target = (
        timedelta(seconds=source_interval),
        timedelta(seconds=request.target_interval_seconds),
    )
    full: list[AggregatedFullBar] = []
    partial: list[ScheduledPartialBar] = []
    gaps: list[AggregateGap] = []
    for window in request.calendar.windows:
        if window.kind != "open":
            continue
        bucket_start = window.start_at
        while bucket_start < window.end_at:
            nominal_end = bucket_start + target
            bucket_end = min(nominal_end, window.end_at)
            starts: list[datetime] = []
            cursor = bucket_start
            while cursor < bucket_end:
                starts.append(cursor)
                cursor += step
            components = [
                by_start[s] for s in starts if s in by_start and by_start[s].end_at == s + step
            ]
            short = bucket_end < nominal_end
            if len(components) != len(starts):
                reason: Literal["UNSCHEDULED_PARTIAL", "MISSING_COMPONENT"] = (
                    "UNSCHEDULED_PARTIAL" if not short else "MISSING_COMPONENT"
                )
                gaps.append(
                    AggregateGap(
                        start_at=bucket_start,
                        end_at=bucket_end,
                        reason=reason,
                        expected_component_starts=tuple(starts),
                    )
                )
            elif short and request.partial_policy == "reject":
                gaps.append(
                    AggregateGap(
                        start_at=bucket_start,
                        end_at=bucket_end,
                        reason="SCHEDULED_PARTIAL_REJECTED",
                        expected_component_starts=tuple(starts),
                    )
                )
            else:
                if short:
                    partial.append(
                        ScheduledPartialBar(
                            target_interval_seconds=request.target_interval_seconds,
                            start_at=bucket_start,
                            end_at=bucket_end,
                            open=components[0].open,
                            high=max(b.high for b in components),
                            low=min(b.low for b in components),
                            close=components[-1].close,
                            volume=sum((b.volume for b in components), Decimal(0)),
                            source_bar_record_ids=tuple(b.bar_record_id for b in components),
                        )
                    )
                else:
                    full.append(
                        AggregatedFullBar(
                            target_interval_seconds=request.target_interval_seconds,
                            start_at=bucket_start,
                            end_at=bucket_end,
                            open=components[0].open,
                            high=max(b.high for b in components),
                            low=min(b.low for b in components),
                            close=components[-1].close,
                            volume=sum((b.volume for b in components), Decimal(0)),
                            source_bar_record_ids=tuple(b.bar_record_id for b in components),
                        )
                    )
            bucket_start = bucket_end
    return AggregateResult(
        full_bars=tuple(full), scheduled_partial_bars=tuple(partial), rejected_gaps=tuple(gaps)
    )
