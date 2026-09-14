from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid7

import pytest
from pydantic import ValidationError

from familytrade.market_data.aggregate import aggregate_completed_bars
from familytrade.market_data.calendar import (
    classify_calendar_interval,
    expected_bar_starts,
    validate_calendar,
)
from familytrade.market_data.models import (
    AggregateRequest,
    CalendarVersion,
    CalendarWindow,
    CausalCommittedSelection,
    CausalLatestRead,
    CompletedBar,
    MarketDataError,
)


def calendar(minutes: int = 7) -> CalendarVersion:
    start = datetime(2026, 11, 1, 23, tzinfo=UTC)
    return CalendarVersion(
        calendar_id=str(uuid7()),
        owner_user_id=str(uuid7()),
        calendar_version=1,
        exchange_timezone="America/Chicago",
        coverage_start=start,
        coverage_end=start + timedelta(minutes=minutes + 1),
        windows=(
            CalendarWindow(
                ordinal=0,
                kind="open",
                start_at=start,
                end_at=start + timedelta(minutes=minutes),
                trading_day=date(2026, 11, 2),
                reason=None,
            ),
            CalendarWindow(
                ordinal=1,
                kind="maintenance",
                start_at=start + timedelta(minutes=minutes),
                end_at=start + timedelta(minutes=minutes + 1),
                trading_day=None,
                reason="BREAK",
            ),
        ),
        metadata_as_of=start,
        provenance_ref="synthetic",
        created_at=start,
        record_version=1,
    )


def bars(cal: CalendarVersion):
    values = []
    for i, start in enumerate(expected_bar_starts(cal, cal.coverage_start, cal.coverage_end, 60)):
        price = Decimal(2000 + i)
        values.append(
            CompletedBar(
                bar_record_id=str(uuid7()),
                owner_user_id=cal.owner_user_id,
                source="synthetic",
                price_basis="trades",
                contract_id=str(uuid7()),
                interval_seconds=60,
                start_at=start,
                end_at=start + timedelta(minutes=1),
                open=price,
                high=price + 1,
                low=price - 1,
                close=price + Decimal("0.5"),
                volume=Decimal(i + 1),
                source_revision=1,
                received_at=start + timedelta(minutes=1),
                completed_at=start + timedelta(minutes=1),
                quality="valid",
                supersedes_bar_record_id=None,
                payload_hash="a" * 64,
                created_at=start + timedelta(minutes=1),
                record_version=1,
            )
        )
    return tuple(values)


def test_dst_session_mapping_uses_materialized_utc_calendar_fixture(contract_case) -> None:
    case = contract_case("dst_session_mapping_uses_materialized_utc_calendar")
    expected = case["expected"]
    fall = calendar()
    spring_start = datetime.fromisoformat("2026-03-08T22:00:00+00:00")
    spring = fall.model_copy(
        update={
            "coverage_start": spring_start,
            "coverage_end": spring_start + timedelta(minutes=8),
            "windows": tuple(
                window.model_copy(
                    update={
                        "start_at": spring_start + (window.start_at - fall.coverage_start),
                        "end_at": spring_start + (window.end_at - fall.coverage_start),
                        "trading_day": date(2026, 3, 9) if window.kind == "open" else None,
                    }
                )
                for window in fall.windows
            ),
        }
    )
    fall_starts = expected_bar_starts(fall, fall.coverage_start, fall.coverage_end, 60)
    spring_starts = expected_bar_starts(spring, spring.coverage_start, spring.coverage_end, 60)

    def interval(starts):
        return [
            starts[0].isoformat().replace("+00:00", "Z"),
            (starts[0] + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        ]

    assert interval(fall_starts) == expected["fall_first_bar"]
    assert interval(spring_starts) == expected["spring_first_bar"]
    assert len(fall_starts) - len(set(fall_starts)) == expected["duplicate_utc_bars_from_fold"]
    invented_break_bars = sum(
        fall.windows[1].start_at <= start < fall.windows[1].end_at for start in fall_starts
    )
    assert invented_break_bars == expected["invented_break_bars"]
    assert (
        classify_calendar_interval(fall, fall.windows[1].start_at, fall.windows[1].end_at)
        == "maintenance"
    )


def test_aggregation_full_bucket_ohlcv_and_component_order() -> None:
    cal = calendar()
    result = aggregate_completed_bars(
        AggregateRequest(
            source_bars=bars(cal),
            calendar=cal,
            target_interval_seconds=300,
            partial_policy="scheduled_partial",
        )
    )
    full = result.full_bars[0]
    assert (full.open, full.high, full.low, full.close, full.volume) == (
        Decimal(2000),
        Decimal(2005),
        Decimal(1999),
        Decimal("2004.5"),
        Decimal(15),
    )
    assert len(full.source_bar_record_ids) == 5


def test_scheduled_partial_is_distinct_and_unpersisted_and_unscheduled_partial_rejected() -> None:
    cal = calendar()
    allow = aggregate_completed_bars(
        AggregateRequest(
            source_bars=bars(cal),
            calendar=cal,
            target_interval_seconds=300,
            partial_policy="scheduled_partial",
        )
    )
    reject = aggregate_completed_bars(
        AggregateRequest(
            source_bars=bars(cal),
            calendar=cal,
            target_interval_seconds=300,
            partial_policy="reject",
        )
    )
    assert (
        len(allow.scheduled_partial_bars) == 1
        and allow.scheduled_partial_bars[0].scheduled_partial is True
    )
    assert reject.rejected_gaps[-1].reason == "SCHEDULED_PARTIAL_REJECTED"


def test_maintenance_holiday_early_close_and_overnight_session_are_not_missing() -> None:
    cal = calendar()
    assert all(
        start < cal.windows[1].start_at
        for start in expected_bar_starts(cal, cal.coverage_start, cal.coverage_end, 60)
    )


def test_materialized_calendar_rejects_overlap_gap_unsorted_and_out_of_coverage_bar() -> None:
    valid = calendar()
    overlap = valid.model_copy(
        update={
            "windows": (
                valid.windows[0],
                valid.windows[1].model_copy(
                    update={"start_at": valid.windows[0].end_at - timedelta(seconds=1)}
                ),
            )
        }
    )
    gap = valid.model_copy(
        update={
            "windows": (
                valid.windows[0],
                valid.windows[1].model_copy(
                    update={"start_at": valid.windows[0].end_at + timedelta(seconds=1)}
                ),
            )
        }
    )
    unsorted = valid.model_copy(update={"windows": tuple(reversed(valid.windows))})
    for invalid in (overlap, gap, unsorted):
        with pytest.raises(MarketDataError):
            validate_calendar(invalid)
    with pytest.raises(MarketDataError):
        expected_bar_starts(
            valid,
            valid.coverage_start - timedelta(minutes=1),
            valid.coverage_start,
            60,
        )


def test_adjacent_open_segments_with_different_trading_day_labels_remain_distinct_anchors() -> None:
    base = calendar(4)
    start = base.coverage_start
    split = base.model_copy(
        update={
            "coverage_end": start + timedelta(minutes=4),
            "windows": (
                CalendarWindow(
                    ordinal=0,
                    kind="open",
                    start_at=start,
                    end_at=start + timedelta(minutes=2),
                    trading_day=date(2026, 11, 2),
                    reason=None,
                ),
                CalendarWindow(
                    ordinal=1,
                    kind="open",
                    start_at=start + timedelta(minutes=2),
                    end_at=start + timedelta(minutes=4),
                    trading_day=date(2026, 11, 3),
                    reason=None,
                ),
            ),
        }
    )
    validate_calendar(split)
    result = aggregate_completed_bars(
        AggregateRequest(
            source_bars=bars(split),
            calendar=split,
            target_interval_seconds=300,
            partial_policy="scheduled_partial",
        )
    )
    assert [item.start_at for item in result.scheduled_partial_bars] == [
        start,
        start + timedelta(minutes=2),
    ]


def test_aggregation_requires_source_strictly_smaller_than_target() -> None:
    cal = calendar()
    five_minute = bars(cal)[0].model_copy(
        update={
            "interval_seconds": 300,
            "end_at": cal.coverage_start + timedelta(minutes=5),
            "completed_at": cal.coverage_start + timedelta(minutes=5),
            "received_at": cal.coverage_start + timedelta(minutes=5),
            "created_at": cal.coverage_start + timedelta(minutes=5),
        }
    )
    with pytest.raises(MarketDataError):
        aggregate_completed_bars(
            AggregateRequest(
                source_bars=(five_minute,),
                calendar=cal,
                target_interval_seconds=300,
            )
        )


def test_aggregation_anchors_each_open_segment_and_never_crosses_break_or_trading_day_label() -> (
    None
):
    base = calendar(4)
    start = base.coverage_start
    split = base.model_copy(
        update={
            "coverage_end": start + timedelta(minutes=4),
            "windows": (
                CalendarWindow(
                    ordinal=0,
                    kind="open",
                    start_at=start,
                    end_at=start + timedelta(minutes=2),
                    trading_day=date(2026, 11, 2),
                    reason=None,
                ),
                CalendarWindow(
                    ordinal=1,
                    kind="open",
                    start_at=start + timedelta(minutes=2),
                    end_at=start + timedelta(minutes=4),
                    trading_day=date(2026, 11, 3),
                    reason=None,
                ),
            ),
        }
    )
    result = aggregate_completed_bars(
        AggregateRequest(
            source_bars=bars(split),
            calendar=split,
            target_interval_seconds=300,
            partial_policy="scheduled_partial",
        )
    )
    assert tuple(item.start_at for item in result.scheduled_partial_bars) == (
        start,
        start + timedelta(minutes=2),
    )
    assert all(
        item.end_at <= split.windows[0].end_at or item.start_at >= split.windows[1].start_at
        for item in result.scheduled_partial_bars
    )


def test_causal_committed_selection_bounds_owner_time_uniqueness_and_observation_order() -> None:
    start = datetime(2026, 11, 1, 23, tzinfo=UTC)
    selection = CausalCommittedSelection(
        start_at=start,
        bar_record_id=str(uuid7()),
        committed_at=start + timedelta(minutes=1),
    )
    with pytest.raises(ValidationError):
        CausalLatestRead(
            known_at=start + timedelta(minutes=2),
            committed_through=start + timedelta(minutes=1),
            committed_selections=(selection, selection),
        )
    with pytest.raises(ValidationError):
        CausalLatestRead(
            known_at=start,
            committed_selections=(selection,),
        )
