"""Materialized UTC calendar operations and interval classification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from familytrade.access.models import UserContext
from familytrade.market_data.models import (
    CalendarCreateInput,
    CalendarVersion,
    CalendarVersionAppendInput,
    MarketDataCode,
    MarketDataError,
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset() != timedelta(0):
        raise MarketDataError(MarketDataCode.VALIDATION_ERROR, "UTC timestamp required.", 422)
    return value.astimezone(UTC)


def validate_calendar(value: CalendarCreateInput | CalendarVersionAppendInput) -> None:
    try:
        ZoneInfo(value.exchange_timezone)
    except ZoneInfoNotFoundError:
        raise MarketDataError(
            MarketDataCode.VALIDATION_ERROR, "Unknown IANA timezone.", 422
        ) from None
    start, end = _utc(value.coverage_start), _utc(value.coverage_end)
    if start >= end:
        raise MarketDataError(MarketDataCode.VALIDATION_ERROR, "Invalid calendar coverage.", 422)
    cursor = start
    previous: tuple[str, object, object] | None = None
    for window in value.windows:
        ws, we = _utc(window.start_at), _utc(window.end_at)
        if ws != cursor or ws >= we or we > end:
            raise MarketDataError(
                MarketDataCode.VALIDATION_ERROR,
                "Calendar windows must exactly and contiguously cover the range.",
                422,
            )
        if window.kind == "open":
            valid = window.trading_day is not None and window.reason is None
        else:
            valid = (
                window.trading_day is None
                and window.reason is not None
                and 1 <= len(window.reason) <= 128
                and not any(ord(c) < 32 or ord(c) == 127 for c in window.reason)
            )
        if not valid:
            raise MarketDataError(
                MarketDataCode.VALIDATION_ERROR, "Invalid calendar window classification.", 422
            )
        classification = (window.kind, window.trading_day, window.reason)
        if classification == previous:
            raise MarketDataError(
                MarketDataCode.VALIDATION_ERROR,
                "Adjacent identical calendar windows must be coalesced.",
                422,
            )
        previous, cursor = classification, we
    if cursor != end:
        raise MarketDataError(
            MarketDataCode.VALIDATION_ERROR, "Calendar coverage contains a gap.", 422
        )


def classify_calendar_interval(
    calendar: CalendarVersion, start_at: datetime, end_at: datetime
) -> str:
    start, end = _utc(start_at), _utc(end_at)
    if start >= end or start < calendar.coverage_start or end > calendar.coverage_end:
        return "outside_materialized_coverage"
    matches = [w for w in calendar.windows if w.start_at <= start and end <= w.end_at]
    return matches[0].kind if len(matches) == 1 else "outside_materialized_coverage"


def expected_bar_starts(
    calendar: CalendarVersion,
    coverage_start: datetime,
    coverage_end: datetime,
    interval_seconds: int,
) -> tuple[datetime, ...]:
    start, end = _utc(coverage_start), _utc(coverage_end)
    if (
        interval_seconds <= 0
        or start < calendar.coverage_start
        or end > calendar.coverage_end
        or start >= end
    ):
        raise MarketDataError(
            MarketDataCode.CALENDAR_COVERAGE_MISSING,
            "Range is outside materialized calendar coverage.",
            409,
        )
    delta = timedelta(seconds=interval_seconds)
    result: list[datetime] = []
    for window in calendar.windows:
        if window.kind != "open":
            continue
        cursor = window.start_at
        while cursor + delta <= window.end_at:
            if start <= cursor < end:
                result.append(cursor)
            cursor += delta
    return tuple(result)


def create_calendar(
    catalog: object, context: UserContext, value: CalendarCreateInput, *, idempotency_key: str
) -> CalendarVersion:
    if not isinstance(catalog, _CalendarCatalog):
        raise TypeError("catalog does not implement calendar operations")
    return catalog.create_calendar(context, value, idempotency_key=idempotency_key)


def append_calendar_version(
    catalog: object,
    context: UserContext,
    calendar_id: str,
    value: CalendarVersionAppendInput,
    *,
    idempotency_key: str,
) -> CalendarVersion:
    if not isinstance(catalog, _CalendarCatalog):
        raise TypeError("catalog does not implement calendar operations")
    return catalog.append_calendar_version(
        context, calendar_id, value, idempotency_key=idempotency_key
    )


@runtime_checkable
class _CalendarCatalog(Protocol):
    def create_calendar(
        self, context: UserContext, value: CalendarCreateInput, *, idempotency_key: str
    ) -> CalendarVersion: ...
    def append_calendar_version(
        self,
        context: UserContext,
        calendar_id: str,
        value: CalendarVersionAppendInput,
        *,
        idempotency_key: str,
    ) -> CalendarVersion: ...
