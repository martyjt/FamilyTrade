"""Pure deterministic FT-07 decision and paper-fill state machine."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar, Literal, NoReturn, cast
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter, ValidationError

from familytrade.market_data.models import BarSelection, CalendarWindow, canonical_json_bytes
from familytrade.simulation.fills import BarPrices, ProspectiveFill, protective_fill
from familytrade.simulation.orders import entry_expiry, limit_fill, market_fill_price, tick_round
from familytrade.simulation.risk import (
    commission,
    modeled_total_loss,
    money_round,
    price_pnl,
    stop_fraction_quantity,
)
from familytrade.simulation.state import (
    CompletedBarEvent,
    DataQualityEvent,
    Decision,
    DecisionEvidence,
    EngineCheckpoint,
    EngineConfig,
    EngineError,
    EngineFailure,
    EngineInputEvent,
    EngineRunResult,
    EngineState,
    EngineStepHash,
    EngineStepResult,
    Fill,
    FillEvidence,
    FinishRunEvent,
    IntervalBucket,
    IntervalIndex,
    MarkEvidence,
    OrderIntent,
    OrderState,
    PositionState,
    ProtectiveBracketState,
    ProtectiveOrderState,
    RiskState,
    RunEvent,
    SourceDatasetProvenance,
)
from familytrade.simulation.state import (
    EngineErrorCode as EngineErrorCodeType,
)
from familytrade.strategies.definitions import (
    AtrMultipleStop,
    FixedTicksStop,
    FixedTicksTarget,
    RiskMultipleTarget,
)
from familytrade.strategies.indicators import (
    FeatureBar,
    advance_feature,
    break_runtime,
    initial_runtime,
)
from familytrade.strategies.rules import evaluate_rule
from familytrade.strategies.validation import canonical_definition_sha256

_EVENT_ADAPTER: TypeAdapter[EngineInputEvent] = TypeAdapter(
    EngineInputEvent, config={"strict": True}
)
_TERMINAL = {"FINISHED", "STOPPED", "BLOCKED_UNCLOSED", "BLOCKED_EXPIRY_UNRESOLVED"}


class EngineErrorCode:
    """Private value namespace; the public state surface remains the closed Literal alias."""

    VALIDATION_ERROR: ClassVar[Literal["VALIDATION_ERROR"]] = "VALIDATION_ERROR"
    INVALID_BAR_EVENT: ClassVar[Literal["INVALID_BAR_EVENT"]] = "INVALID_BAR_EVENT"
    EVENT_AFTER_END: ClassVar[Literal["EVENT_AFTER_END"]] = "EVENT_AFTER_END"
    BATCH_LIMIT_EXCEEDED: ClassVar[Literal["BATCH_LIMIT_EXCEEDED"]] = "BATCH_LIMIT_EXCEEDED"
    UNSUPPORTED_CONFIGURATION: ClassVar[Literal["UNSUPPORTED_CONFIGURATION"]] = (
        "UNSUPPORTED_CONFIGURATION"
    )
    UNSUPPORTED_FILL_INTERVAL: ClassVar[Literal["UNSUPPORTED_FILL_INTERVAL"]] = (
        "UNSUPPORTED_FILL_INTERVAL"
    )
    UNSUPPORTED_CONTRACT_CHANGE: ClassVar[Literal["UNSUPPORTED_CONTRACT_CHANGE"]] = (
        "UNSUPPORTED_CONTRACT_CHANGE"
    )
    CONFIG_MISMATCH: ClassVar[Literal["CONFIG_MISMATCH"]] = "CONFIG_MISMATCH"
    CHECKPOINT_MISMATCH: ClassVar[Literal["CHECKPOINT_MISMATCH"]] = "CHECKPOINT_MISMATCH"
    EVENT_OUT_OF_ORDER: ClassVar[Literal["EVENT_OUT_OF_ORDER"]] = "EVENT_OUT_OF_ORDER"
    DUPLICATE_CONFLICT: ClassVar[Literal["DUPLICATE_CONFLICT"]] = "DUPLICATE_CONFLICT"
    RUN_FINISHED: ClassVar[Literal["RUN_FINISHED"]] = "RUN_FINISHED"


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _state_hash(state: EngineState) -> str:
    return _hash(state)


def _config_hash(config: EngineConfig) -> str:
    return _hash(config)


def _config_snapshot(config: EngineConfig) -> EngineConfig:
    original = canonical_json_bytes(config)
    if len(original) > 16_777_216:
        _unsupported("", "CONFIG_SNAPSHOT_LIMIT")
    snapshot = EngineConfig.model_validate(
        config.model_dump(mode="python", round_trip=True), strict=True
    )
    if canonical_json_bytes(snapshot) != original:
        _unsupported("", "CONFIG_SNAPSHOT_LIMIT")
    return snapshot


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _config_diff_pointers(expected: EngineConfig, actual: EngineConfig) -> tuple[str, ...]:
    left = json.loads(canonical_json_bytes(expected))
    right = json.loads(canonical_json_bytes(actual))
    pointers: list[str] = []

    def compare(a: object, b: object, path: str) -> None:
        if type(a) is not type(b):
            pointers.append(path)
        elif isinstance(a, dict) and isinstance(b, dict):
            keys = set(a) | set(b)
            for key in keys:
                child = f"{path}/{_pointer_token(key)}"
                if key not in a or key not in b:
                    pointers.append(child)
                else:
                    compare(a[key], b[key], child)
        elif isinstance(a, list) and isinstance(b, list):
            for index in range(max(len(a), len(b))):
                child = f"{path}/{index}"
                if index >= len(a) or index >= len(b):
                    pointers.append(child)
                else:
                    compare(a[index], b[index], child)
        elif a != b:
            pointers.append(path)

    compare(left, right, "")
    return tuple(sorted(pointers))


def deterministic_uuid7(domain: str, timestamp: datetime, fields: tuple[object, ...]) -> str:
    """Derive the contract UUIDv7 vector without clock or randomness."""
    if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
        raise ValueError("UUID timestamp must be aware UTC")
    digest = hashlib.sha256(canonical_json_bytes(["paper-engine-v1", domain, *fields])).digest()
    milliseconds = int(timestamp.timestamp() * 1000)
    raw = bytearray(milliseconds.to_bytes(6, "big") + digest[:10])
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def _raise(code: EngineErrorCodeType | str, detail: str | dict[str, Any] | None = None) -> NoReturn:
    now = datetime(1970, 1, 1, tzinfo=UTC)
    if isinstance(detail, dict):
        error = EngineError.model_validate(
            {"code": code, "message": _ERROR_MESSAGES[code], "details": detail}, strict=True
        )
        raise EngineFailure(error)
    reason = detail or "INPUT_CROSS_FIELD"
    if code == "VALIDATION_ERROR":
        details: dict[str, Any] = {"kind": "validation_error", "path": "", "reason": reason}
    elif code == "UNSUPPORTED_CONFIGURATION":
        details = {
            "kind": "unsupported_configuration",
            "path": "/max_canonical_bars"
            if reason == "BAR_LIMIT_RANGE"
            else "/strategy_version/status",
            "reason": reason if reason != "INPUT_CROSS_FIELD" else "POLICY_UNSUPPORTED",
        }
    elif code == "UNSUPPORTED_FILL_INTERVAL":
        details = {
            "kind": "unsupported_fill_interval",
            "fill_interval_seconds": 1,
            "execution_interval_seconds": 1,
            "reason": "SUBMINUTE",
        }
    elif code == "CONFIG_MISMATCH":
        details = {
            "kind": "config_mismatch",
            "path": "",
            "expected_config_sha256": "0" * 64,
            "actual_config_sha256": "1" * 64,
        }
    elif code == "CHECKPOINT_MISMATCH":
        checkpoint_reason = detail or "STATE_INVARIANT"
        checkpoint_paths = {
            "SCHEMA_VERSION": "/schema_version",
            "FORMAT_VERSION": "/format_version",
            "ENGINE_VERSION": "/engine_version",
            "CHECKPOINT_CONFIG_HASH": "/config_sha256",
            "STATE_HASH": "/state_sha256",
            "STATE_INVARIANT": "/state",
        }
        details = {
            "kind": "checkpoint_mismatch",
            "path": checkpoint_paths[checkpoint_reason],
            "reason": checkpoint_reason,
        }
    elif code == "DUPLICATE_CONFLICT":
        details = {
            "kind": "duplicate_conflict",
            "logical_start_at": now,
            "logical_end_at": now + timedelta(minutes=1),
            "previous_input_sha256": "0" * 64,
            "input_sha256": "1" * 64,
        }
    elif code == "EVENT_OUT_OF_ORDER":
        details = {
            "kind": "event_out_of_order",
            "previous_start_at": now,
            "previous_end_at": now,
            "previous_recorded_at": now,
            "input_start_at": now,
            "input_end_at": now,
            "input_recorded_at": now - timedelta(microseconds=1),
        }
    elif code == "RUN_FINISHED":
        details = {"kind": "run_finished", "status": "FINISHED", "finished_reason": "MARK_OPEN"}
    elif code == "BATCH_LIMIT_EXCEEDED":
        details = {
            "kind": "batch_limit_exceeded",
            "current_count": 0,
            "input_delta": 2,
            "max_canonical_bars": 1,
        }
    elif code == "EVENT_AFTER_END":
        details = {
            "kind": "event_after_end",
            "event_end_at": now + timedelta(minutes=1),
            "config_end_at": now,
        }
    elif code == "INVALID_BAR_EVENT":
        details = {"kind": "invalid_bar_event", "bar_record_id": "unknown", "reason": "IDENTITY"}
    else:
        details = {
            "kind": "unsupported_contract_change",
            "expected_contract_id": "018f4c00-0000-7000-8000-000000000001",
            "actual_contract_id": "018f4c00-0000-7000-8000-000000000002",
            "expected_record_version": 1,
            "actual_record_version": 2,
        }
    error = EngineError.model_validate(
        {"code": code, "message": _ERROR_MESSAGES[code], "details": details}, strict=True
    )
    raise EngineFailure(error)


def _unsupported(path: str, reason: str) -> NoReturn:
    _raise(
        EngineErrorCode.UNSUPPORTED_CONFIGURATION,
        {"kind": "unsupported_configuration", "path": path, "reason": reason},
    )


def _validation(path: str, reason: str) -> NoReturn:
    _raise(
        EngineErrorCode.VALIDATION_ERROR,
        {"kind": "validation_error", "path": path, "reason": reason},
    )


_ERROR_MESSAGES: dict[str, str] = {
    "VALIDATION_ERROR": "Engine input is invalid.",
    "INVALID_BAR_EVENT": "Bar event is invalid.",
    "EVENT_AFTER_END": "Bar event is after the run end.",
    "BATCH_LIMIT_EXCEEDED": "Engine batch bar limit is exceeded.",
    "UNSUPPORTED_CONFIGURATION": "Engine configuration is unsupported.",
    "UNSUPPORTED_FILL_INTERVAL": "Fill interval is unsupported.",
    "UNSUPPORTED_CONTRACT_CHANGE": "Contract change is unsupported.",
    "CONFIG_MISMATCH": "Engine configuration does not match state.",
    "CHECKPOINT_MISMATCH": "Engine checkpoint is invalid.",
    "EVENT_OUT_OF_ORDER": "Engine event is out of order.",
    "DUPLICATE_CONFLICT": "Logical bar has conflicting content.",
    "RUN_FINISHED": "Engine run is already terminal.",
}


def _five_year_limit(start: datetime) -> datetime:
    try:
        return start.replace(year=start.year + 5)
    except ValueError:
        return start.replace(year=start.year + 5, day=28)


def _validate_config(config: EngineConfig) -> None:
    strategy = config.strategy_version
    contract = config.contract
    calendar = config.calendar
    if config.fill_model.fill_interval_seconds != strategy.fill_interval_seconds:
        _unsupported("/fill_model/fill_interval_seconds", "FILL_INTERVAL_STRATEGY_MISMATCH")
    nested_owners = (
        ("/calendar/owner_user_id", calendar.owner_user_id),
        ("/contract/owner_user_id", contract.owner_user_id),
        ("/strategy_version/owner_user_id", strategy.owner_user_id),
    )
    if any(value != config.owner_user_id for _, value in nested_owners):
        _unsupported(
            min(path for path, value in nested_owners if value != config.owner_user_id),
            "OWNER_MISMATCH",
        )
    if strategy.status != "validated":
        _unsupported("/strategy_version/status", "STRATEGY_NOT_EXECUTABLE")
    if canonical_definition_sha256(strategy.definition) != strategy.canonical_definition_sha256:
        _unsupported("/strategy_version/canonical_definition_sha256", "STRATEGY_HASH_MISMATCH")
    if strategy.catalogue_version != "feature-catalogue-v1":
        _unsupported("/strategy_version/catalogue_version", "CATALOGUE_MISMATCH")
    if contract.calendar_id != calendar.calendar_id:
        _unsupported("/contract/calendar_id", "CONTRACT_CALENDAR_MISMATCH")
    if contract.calendar_version != calendar.calendar_version:
        _unsupported("/contract/calendar_version", "CONTRACT_CALENDAR_MISMATCH")
    if contract.tick_size <= 0:
        _unsupported("/contract/tick_size", "CONTRACT_NUMERIC_POLICY")
    if contract.multiplier <= 0:
        _unsupported("/contract/multiplier", "CONTRACT_NUMERIC_POLICY")
    currencies = (
        ("/base_currency", config.base_currency),
        ("/contract/currency", contract.currency),
        ("/cost_model/currency", config.cost_model.currency),
    )
    if any(value != "USD" for _, value in currencies):
        _unsupported(min(path for path, value in currencies if value != "USD"), "CURRENCY_MISMATCH")
    revision = config.dataset_revision
    if config.mode == "backtest" and (revision is None or config.end_at is None):
        _unsupported("/dataset_revision" if revision is None else "/end_at", "BACKTEST_BINDING")
    if config.mode == "forward_paper" and (revision is not None or config.end_at is not None):
        _unsupported("/dataset_revision" if revision is not None else "/end_at", "FORWARD_BINDING")
    if config.mode == "backtest":
        assert revision is not None and config.end_at is not None
        binding_checks = (
            ("/dataset_revision/calendar_id", revision.calendar_id == calendar.calendar_id),
            (
                "/dataset_revision/calendar_version",
                revision.calendar_version == calendar.calendar_version,
            ),
            (
                "/dataset_revision/contract_version",
                revision.contract_version == contract.record_version,
            ),
            ("/dataset_revision/owner_user_id", revision.owner_user_id == config.owner_user_id),
            ("/dataset_revision/published_at", revision.published_at is not None),
            (
                "/dataset_revision/series_key/contract_id",
                revision.series_key.contract_id == contract.contract_id,
            ),
            (
                "/dataset_revision/series_key/interval_seconds",
                revision.series_key.interval_seconds == 60,
            ),
            (
                "/dataset_revision/series_key/owner_user_id",
                revision.series_key.owner_user_id == config.owner_user_id,
            ),
            (
                "/dataset_revision/series_key/price_basis",
                revision.series_key.price_basis == config.price_basis,
            ),
            (
                "/dataset_revision/series_key/source",
                revision.series_key.source == config.source,
            ),
            ("/dataset_revision/status", revision.status == "published"),
        )
        failed_binding = [path for path, okay in binding_checks if not okay]
        if failed_binding:
            _unsupported(min(failed_binding), "DATASET_BINDING")
        coverage_checks = (
            ("/calendar/coverage_end", revision.coverage_end <= calendar.coverage_end),
            ("/calendar/coverage_start", calendar.coverage_start <= revision.coverage_start),
            ("/dataset_revision/coverage_end", config.end_at <= revision.coverage_end),
            ("/dataset_revision/coverage_start", revision.coverage_start <= config.start_at),
            ("/end_at", config.start_at < config.end_at),
        )
        failed_coverage = [path for path, okay in coverage_checks if not okay]
        if failed_coverage:
            _unsupported(min(failed_coverage), "DATASET_COVERAGE")
        if config.end_at > _five_year_limit(config.start_at):
            _unsupported("/end_at", "RUN_SPAN_EXCEEDED")
        if config.end_at >= contract.liquidation_start_at and (
            config.end_at < contract.last_trade_at
            or revision.coverage_end < contract.last_trade_at
            or calendar.coverage_end < contract.last_trade_at
        ):
            failed = []
            if calendar.coverage_end < contract.last_trade_at:
                failed.append("/calendar/coverage_end")
            if revision.coverage_end < contract.last_trade_at:
                failed.append("/dataset_revision/coverage_end")
            if config.end_at < contract.last_trade_at:
                failed.append("/end_at")
            _unsupported(min(failed), "DATASET_COVERAGE")
    if not 1 <= config.max_canonical_bars <= 2_000_000:
        _raise(EngineErrorCode.UNSUPPORTED_CONFIGURATION, "BAR_LIMIT_RANGE")
    if len(canonical_json_bytes(config)) > 16_777_216:
        _unsupported("", "CONFIG_SNAPSHOT_LIMIT")
    if (config.mode == "backtest" and config.lane_id is not None) or (
        config.mode == "forward_paper" and config.lane_id is None
    ):
        _unsupported("/lane_id", "MODE_LANE_MISMATCH")
    if config.mode == "forward_paper" and (
        config.end_policy != "mark_open" or config.force_close_at is not None
    ):
        _unsupported("/force_close_at", "END_POLICY_MISMATCH")
    if config.mode == "backtest":
        assert config.end_at is not None
        expected_force = config.end_at - timedelta(seconds=config.fill_model.fill_interval_seconds)
        if (config.end_policy == "force_close" and config.force_close_at != expected_force) or (
            config.end_policy == "mark_open" and config.force_close_at is not None
        ):
            _unsupported("/force_close_at", "END_POLICY_MISMATCH")
    if (
        config.random_seed is not None
        or config.fill_model.both_hit_policy != strategy.definition.order_policy.both_hit_policy
        or config.fill_model.entry_bar_exit_policy
        != strategy.definition.order_policy.entry_bar_exit_policy
    ):
        policy_paths = []
        if config.random_seed is not None:
            policy_paths.append("/random_seed")
        if config.fill_model.both_hit_policy != strategy.definition.order_policy.both_hit_policy:
            policy_paths.append("/fill_model/both_hit_policy")
        if (
            config.fill_model.entry_bar_exit_policy
            != strategy.definition.order_policy.entry_bar_exit_policy
        ):
            policy_paths.append("/fill_model/entry_bar_exit_policy")
        _unsupported(min(policy_paths), "POLICY_UNSUPPORTED")
    fill_interval = config.fill_model.fill_interval_seconds
    execution = strategy.execution_interval_seconds
    if (
        fill_interval < 60
        or fill_interval % 60
        or fill_interval > execution
        or execution % fill_interval
    ):
        reason = (
            "SUBMINUTE"
            if fill_interval < 60
            else "NON_MINUTE"
            if fill_interval % 60
            else "LARGER_THAN_EXECUTION"
            if fill_interval > execution
            else "NOT_DIVISOR"
        )
        _raise(
            EngineErrorCode.UNSUPPORTED_FILL_INTERVAL,
            {
                "kind": "unsupported_fill_interval",
                "fill_interval_seconds": fill_interval,
                "execution_interval_seconds": execution,
                "reason": reason,
            },
        )
    if not calendar.coverage_start <= config.start_at < calendar.coverage_end:
        _unsupported("/start_at", "DATASET_COVERAGE")
    for window in (*config.entry_windows, *strategy.definition.constraints.entry_windows):
        if (
            window.calendar_id != calendar.calendar_id
            or window.calendar_version != calendar.calendar_version
        ):
            _unsupported(
                "/entry_windows"
                if window in config.entry_windows
                else "/strategy_version/definition/constraints/entry_windows",
                "CONTRACT_CALENDAR_MISMATCH",
            )


def initialize_engine(config: EngineConfig) -> EngineState:
    _validate_config(config)
    snapshot = _config_snapshot(config)
    runtimes = tuple(
        sorted(
            (initial_runtime(feature) for feature in config.strategy_version.definition.features),
            key=lambda item: item.feature_id,
        )
    )
    zone_intervals = tuple(
        sorted(
            {
                module.zone_interval_seconds
                for module in config.strategy_version.definition.setup_modules
                if module.kind == "confirmed_pivot_zones_v1"
            }
        )
    )
    baseline_day = _trading_day(config.calendar.windows, config.start_at)
    return EngineState(
        schema_version="v1",
        engine_version="paper-engine-v1",
        config_snapshot=snapshot,
        config_sha256=_config_hash(snapshot),
        status="ACTIVE",
        last_input_kind=None,
        last_input_start_at=None,
        last_input_end_at=None,
        last_bar_start_at=None,
        last_bar_record_id=None,
        last_bar_payload_hash=None,
        last_availability_at=None,
        last_recorded_at=None,
        last_input_sha256=None,
        last_published_base_revision_id=None,
        canonical_bar_count=0,
        last_source_slot_index=None,
        last_fill_slot_index=None,
        last_execution_slot_index=None,
        zone_slot_indexes=tuple(
            IntervalIndex(interval_seconds=item, last_slot_index=None) for item in zone_intervals
        ),
        interval_buckets=(),
        feature_values=(),
        feature_runtime=runtimes,
        zones=(),
        setups=(),
        pending_entry=None,
        position=None,
        close_intent=None,
        scheduled_force_close=None,
        cash=money_round(config.starting_cash),
        equity=money_round(config.starting_cash),
        fees=Decimal("0.00"),
        realized_pnl=Decimal("0.00"),
        unrealized_pnl=Decimal("0.00"),
        exposure_seconds=0,
        last_mark=None,
        last_mark_bar_record_id=None,
        last_mark_source_bar_record_ids=(),
        last_mark_source_dataset_provenance=(),
        mark_status="none",
        risk=RiskState(
            trading_day=baseline_day,
            daily_start_equity=money_round(config.starting_cash),
            filled_entries_in_trading_day=0,
            latches=(),
            high_water=money_round(config.starting_cash),
            current_drawdown=Decimal("0.00"),
            maximum_drawdown=Decimal("0.00"),
        ),
        next_decision_sequence=0,
        next_order_sequence=0,
        next_fill_sequence=0,
        next_event_sequence=0,
        next_zone_sequence=0,
        next_setup_sequence=0,
        next_correlation_sequence=0,
        force_close_state="waiting" if config.end_policy == "force_close" else "not_applicable",
        finished_reason=None,
    )


def _event_projection(
    event: CompletedBarEvent | DataQualityEvent | FinishRunEvent,
) -> dict[str, Any]:
    value = event.model_dump(mode="python")
    emission = cast(dict[str, Any], value["emission_context"])
    del emission["attempt_id"]
    del emission["fencing_token"]
    return value


def _identity(
    event: CompletedBarEvent | DataQualityEvent | FinishRunEvent,
) -> tuple[datetime, datetime, datetime]:
    if isinstance(event, CompletedBarEvent):
        return (
            event.selection.bar.start_at,
            event.selection.bar.end_at,
            event.selection.availability_at,
        )
    if isinstance(event, DataQualityEvent):
        return event.start_at, event.end_at, event.end_at
    return event.effective_at, event.effective_at, event.effective_at


def _empty(state: EngineState, replayed: bool = False) -> EngineStepResult:
    state_sha = _state_hash(state)
    return EngineStepResult(
        state=state,
        pre_state_sha256=state_sha,
        post_state_sha256=state_sha,
        decisions=(),
        intents=(),
        protective_orders=(),
        fills=(),
        fill_evidence=(),
        mark_evidence=(),
        events=(),
        replayed=replayed,
    )


def _validate_state_config(config: EngineConfig, state: EngineState) -> None:
    expected_contract = state.config_snapshot.contract
    actual_contract = config.contract
    if (
        expected_contract.contract_id != actual_contract.contract_id
        or expected_contract.record_version != actual_contract.record_version
    ):
        _raise(
            EngineErrorCode.UNSUPPORTED_CONTRACT_CHANGE,
            {
                "kind": "unsupported_contract_change",
                "expected_contract_id": expected_contract.contract_id,
                "actual_contract_id": actual_contract.contract_id,
                "expected_record_version": expected_contract.record_version,
                "actual_record_version": actual_contract.record_version,
            },
        )
    snapshot_hash = _config_hash(state.config_snapshot)
    actual_hash = _config_hash(config)
    pointers = _config_diff_pointers(state.config_snapshot, config)
    if pointers:
        _raise(
            EngineErrorCode.CONFIG_MISMATCH,
            {
                "kind": "config_mismatch",
                "path": pointers[0],
                "expected_config_sha256": state.config_sha256,
                "actual_config_sha256": actual_hash,
            },
        )
    if state.config_sha256 != snapshot_hash:
        _raise(
            EngineErrorCode.CONFIG_MISMATCH,
            {
                "kind": "config_mismatch",
                "path": "",
                "expected_config_sha256": state.config_sha256,
                "actual_config_sha256": snapshot_hash,
            },
        )


def _open_window(windows: tuple[CalendarWindow, ...], instant: datetime) -> CalendarWindow | None:
    return next(
        (
            window
            for window in windows
            if window.kind == "open" and window.start_at <= instant < window.end_at
        ),
        None,
    )


def _trading_day(windows: tuple[CalendarWindow, ...], instant: datetime) -> date | None:
    window = next((item for item in windows if item.start_at <= instant < item.end_at), None)
    return window.trading_day if window else None


def _parse_local(value: str) -> tuple[int, int, int]:
    parts = tuple(int(item) for item in value.split(":"))
    padded = parts + (0, 0)
    return padded[0], padded[1], padded[2]


def _one_window_allows(config: EngineConfig, instant: datetime, windows: tuple[Any, ...]) -> bool:
    if not windows:
        return True
    local = instant.astimezone(ZoneInfo(config.calendar.exchange_timezone))
    seconds = local.hour * 3600 + local.minute * 60 + local.second
    for item in windows:
        if local.weekday() not in item.days_of_week:
            continue
        start = _parse_local(item.start_local)
        end = _parse_local(item.end_local)
        start_s = start[0] * 3600 + start[1] * 60 + start[2]
        end_s = end[0] * 3600 + end[1] * 60 + end[2]
        if (
            (start_s <= seconds < end_s)
            if start_s < end_s
            else (seconds >= start_s or seconds < end_s)
        ):
            return True
    return False


def _entry_window_allows(config: EngineConfig, instant: datetime) -> bool:
    return (
        _open_window(config.calendar.windows, instant) is not None
        and _one_window_allows(config, instant, config.entry_windows)
        and _one_window_allows(
            config, instant, config.strategy_version.definition.constraints.entry_windows
        )
    )


def _first_entry_window_close(
    config: EngineConfig, start: datetime, end: datetime
) -> datetime | None:
    if not _entry_window_allows(config, start):
        return start
    cursor = start + timedelta(seconds=1)
    while cursor <= end:
        if not _entry_window_allows(config, cursor):
            return cursor
        cursor += timedelta(seconds=1)
    return None


def _bucket_bounds(
    config: EngineConfig, instant: datetime, interval: int
) -> tuple[datetime, datetime] | None:
    window = _open_window(config.calendar.windows, instant)
    if window is None:
        return None
    elapsed = int((instant - window.start_at).total_seconds())
    start = window.start_at + timedelta(seconds=(elapsed // interval) * interval)
    end = start + timedelta(seconds=interval)
    if end > window.end_at:
        return None
    return start, end


def _required_intervals(config: EngineConfig) -> tuple[int, ...]:
    intervals = {
        60,
        config.fill_model.fill_interval_seconds,
        config.strategy_version.execution_interval_seconds,
    }
    intervals.update(
        feature.interval_seconds for feature in config.strategy_version.definition.features
    )
    intervals.update(index.interval_seconds for index in initialize_zone_indexes(config))
    return tuple(sorted(intervals))


def initialize_zone_indexes(config: EngineConfig) -> tuple[IntervalIndex, ...]:
    return tuple(
        IntervalIndex(interval_seconds=module.zone_interval_seconds, last_slot_index=None)
        for module in config.strategy_version.definition.setup_modules
        if module.kind == "confirmed_pivot_zones_v1"
    )


def _aggregate(bucket: IntervalBucket) -> FeatureBar:
    selections = bucket.selections
    provenance = tuple(
        SourceDatasetProvenance(
            bar_record_id=selection.bar.bar_record_id,
            published_base_revision_id=revision,
        )
        for selection, revision in zip(selections, bucket.published_base_revision_ids, strict=True)
    )
    return FeatureBar(
        start_at=bucket.start_at,
        end_at=bucket.end_at,
        open=selections[0].bar.open,
        high=max(item.bar.high for item in selections),
        low=min(item.bar.low for item in selections),
        close=selections[-1].bar.close,
        volume=sum((item.bar.volume for item in selections), Decimal(0)),
        source_bar_record_ids=tuple(item.bar.bar_record_id for item in selections),
        source_dataset_provenance=provenance,
        known_at=max(item.availability_at for item in selections),
        source_availability_at=tuple(item.availability_at for item in selections),
        selections=selections,
    )


def _input_correlation(
    config: EngineConfig,
    state: EngineState,
    event: CompletedBarEvent | DataQualityEvent | FinishRunEvent,
    input_sha: str,
) -> str:
    start, end, modeled = _identity(event)
    return deterministic_uuid7(
        "correlation",
        modeled,
        (
            config.run_id,
            config.lane_id,
            state.next_correlation_sequence,
            event.kind,
            start,
            end,
            input_sha,
        ),
    )


def _emit_event(
    config: EngineConfig,
    state: EngineState,
    event_input: CompletedBarEvent | DataQualityEvent | FinishRunEvent,
    correlation_id: str,
    *,
    event_type: str,
    effective_at: datetime,
    payload: dict[str, Any],
    causation_id: str | None,
) -> tuple[EngineState, RunEvent]:
    sequence = state.next_event_sequence
    updated = state.model_copy(update={"next_event_sequence": sequence + 1})
    state_sha = _state_hash(updated)
    aggregate_type = "run" if config.mode == "backtest" else "lane"
    aggregate_id = config.run_id if config.mode == "backtest" else cast(str, config.lane_id)
    event_id = deterministic_uuid7(
        "run-event",
        effective_at,
        (
            config.run_id,
            config.lane_id,
            aggregate_type,
            aggregate_id,
            sequence,
            event_type,
            _hash(payload),
            causation_id,
            correlation_id,
            state_sha,
        ),
    )
    output_time = event_input.emission_context.output_recorded_at
    return updated, RunEvent(
        schema_version="v1",
        run_event_id=event_id,
        owner_user_id=config.owner_user_id,
        aggregate_type=cast(Any, aggregate_type),
        aggregate_id=aggregate_id,
        sequence=sequence,
        event_type=event_type,
        effective_at=effective_at,
        recorded_at=output_time,
        payload=payload,
        causation_id=causation_id,
        correlation_id=correlation_id,
        state_sha256=state_sha,
        attempt_id=event_input.emission_context.attempt_id,
        fencing_token=event_input.emission_context.fencing_token,
        created_at=output_time,
        record_version=1,
    )


def _event_cursor_state(
    state: EngineState,
    event: CompletedBarEvent | DataQualityEvent | FinishRunEvent,
    input_sha: str,
) -> EngineState:
    start, end, modeled = _identity(event)
    update: dict[str, Any] = {
        "last_input_kind": event.kind,
        "last_input_start_at": start,
        "last_input_end_at": end,
        "last_availability_at": modeled,
        "last_recorded_at": event.emission_context.output_recorded_at,
        "last_input_sha256": input_sha,
        "next_correlation_sequence": state.next_correlation_sequence + 1,
    }
    if isinstance(event, CompletedBarEvent):
        update.update(
            last_bar_start_at=start,
            last_bar_record_id=event.selection.bar.bar_record_id,
            last_bar_payload_hash=event.selection.bar.payload_hash,
            last_published_base_revision_id=event.published_base_revision_id,
        )
    return state.model_copy(update=update)


def _validate_completed(config: EngineConfig, state: EngineState, event: CompletedBarEvent) -> None:
    bar = event.selection.bar
    if event.selection.correction_observations:
        _validation("/selection/correction_observations", "UNSUPPORTED_CORRECTION_OBSERVATION")
    if config.end_at is not None and bar.end_at > config.end_at:
        _raise(
            EngineErrorCode.EVENT_AFTER_END,
            {
                "kind": "event_after_end",
                "event_end_at": bar.end_at,
                "config_end_at": config.end_at,
            },
        )
    revision_id = config.dataset_revision.dataset_revision_id if config.dataset_revision else None
    invalid = (
        bar.owner_user_id != config.owner_user_id
        or bar.source != config.source
        or bar.price_basis != config.price_basis
        or bar.contract_id != config.contract.contract_id
        or bar.interval_seconds != 60
        or bar.end_at - bar.start_at != timedelta(seconds=60)
        or bar.quality != "valid"
        or bar.high < max(bar.open, bar.close)
        or bar.low > min(bar.open, bar.close)
        or bar.low > bar.high
        or bar.volume < 0
        or bar.volume != bar.volume.to_integral_value()
        or any(
            value % config.contract.tick_size for value in (bar.open, bar.high, bar.low, bar.close)
        )
        or event.recorded_at < event.selection.availability_at
        or event.emission_context.output_recorded_at < event.emission_context.order_submitted_at
        or event.emission_context.order_submitted_at < event.recorded_at
        or _open_window(config.calendar.windows, bar.start_at) is None
        or _open_window(config.calendar.windows, bar.end_at - timedelta(microseconds=1)) is None
    )
    if config.mode == "backtest":
        invalid = invalid or (
            event.selection.origin != "archive"
            or event.published_base_revision_id != revision_id
            or event.selection.availability_at != bar.end_at
            or event.recorded_at != bar.end_at
            or event.emission_context.order_submitted_at != bar.end_at
            or event.emission_context.output_recorded_at != bar.end_at
        )
    else:
        invalid = (
            invalid
            or event.selection.origin != "active"
            or event.selection.availability_at < bar.end_at
        )
    if invalid:
        reason = (
            "SELECTION_STATUS"
            if bar.quality != "valid"
            else "IDENTITY"
            if (
                bar.owner_user_id != config.owner_user_id
                or bar.source != config.source
                or bar.price_basis != config.price_basis
                or bar.contract_id != config.contract.contract_id
            )
            else "OHLCV"
            if (
                bar.high < max(bar.open, bar.close)
                or bar.low > min(bar.open, bar.close)
                or bar.low > bar.high
                or bar.volume < 0
                or bar.volume != bar.volume.to_integral_value()
            )
            else "PRICE_TICK"
            if any(
                value % config.contract.tick_size
                for value in (bar.open, bar.high, bar.low, bar.close)
            )
            else "INTERVAL"
            if bar.interval_seconds != 60 or bar.end_at - bar.start_at != timedelta(seconds=60)
            else "AVAILABILITY"
            if (
                event.recorded_at < event.selection.availability_at
                or event.emission_context.output_recorded_at
                < event.emission_context.order_submitted_at
                or event.emission_context.order_submitted_at < event.recorded_at
                or (
                    config.mode == "backtest"
                    and (
                        event.selection.origin != "archive"
                        or event.published_base_revision_id != revision_id
                        or event.selection.availability_at != bar.end_at
                        or event.recorded_at != bar.end_at
                        or event.emission_context.order_submitted_at != bar.end_at
                        or event.emission_context.output_recorded_at != bar.end_at
                    )
                )
                or (
                    config.mode == "forward_paper"
                    and (
                        event.selection.origin != "active"
                        or event.selection.availability_at < bar.end_at
                    )
                )
            )
            else "CALENDAR"
        )
        _raise(
            EngineErrorCode.INVALID_BAR_EVENT,
            {"kind": "invalid_bar_event", "bar_record_id": bar.bar_record_id, "reason": reason},
        )
    if state.last_input_end_at is not None and bar.start_at > state.last_input_end_at:
        # Closed spans still require explicit quality events, making causal coverage auditable.
        _validation("/effective_at", "UNACCOUNTED_OPEN_INTERVAL")


def _validate_quality(config: EngineConfig, state: EngineState, event: DataQualityEvent) -> int:
    valid_combo = (
        (event.status == "missing" and event.reason == "NO_BAR" and not event.source_bar_record_ids)
        or (
            event.status == "invalid"
            and event.reason == "INVALID_BAR"
            and bool(event.source_bar_record_ids)
            and tuple(sorted(set(event.source_bar_record_ids))) == event.source_bar_record_ids
        )
        or (
            event.status == "closed"
            and event.reason in {"MAINTENANCE", "SCHEDULED_CLOSED"}
            and not event.source_bar_record_ids
        )
    )
    if (
        not valid_combo
        or event.start_at >= event.end_at
        or event.start_at.second
        or event.start_at.microsecond
        or event.end_at.second
        or event.end_at.microsecond
        or event.recorded_at < event.end_at
        or (event.end_at - event.start_at).total_seconds() % 60
        or (config.end_at is not None and event.end_at > config.end_at)
    ):
        combination_failures = []
        if not valid_combo:
            combination_failures.append("/status")
        if event.start_at >= event.end_at:
            combination_failures.append("/end_at")
        if event.recorded_at < event.end_at:
            combination_failures.append("/recorded_at")
        _validation(min(combination_failures or ["/start_at"]), "DATA_QUALITY_COMBINATION")
    if config.mode == "backtest" and not (
        event.recorded_at
        == event.end_at
        == event.emission_context.order_submitted_at
        == event.emission_context.output_recorded_at
    ):
        _validation("/emission_context/order_submitted_at", "BACKTEST_TIMING_MISMATCH")
    if state.last_input_end_at is not None and event.start_at > state.last_input_end_at:
        _validation("/effective_at", "UNACCOUNTED_OPEN_INTERVAL")
    if event.status == "closed":
        match = next(
            (
                window
                for window in config.calendar.windows
                if window.start_at <= event.start_at and event.end_at <= window.end_at
            ),
            None,
        )
        expected = "MAINTENANCE" if match and match.kind == "maintenance" else "SCHEDULED_CLOSED"
        if match is None or match.kind == "open" or expected != event.reason:
            _validation("/reason", "DATA_QUALITY_COMBINATION")
        return 0
    seconds = int((event.end_at - event.start_at).total_seconds())
    if any(
        _open_window(config.calendar.windows, event.start_at + timedelta(seconds=i)) is None
        for i in range(0, seconds, 60)
    ):
        _validation("/start_at", "DATA_QUALITY_COMBINATION")
    return seconds // 60


def _order_transition(
    config: EngineConfig,
    state: EngineState,
    event_input: CompletedBarEvent | DataQualityEvent | FinishRunEvent,
    correlation: str,
    order: OrderState | ProtectiveOrderState,
    from_status: str,
    to_status: str,
    reason: str,
    effective_at: datetime,
) -> tuple[EngineState, RunEvent]:
    return _emit_event(
        config,
        state,
        event_input,
        correlation,
        event_type="ORDER_STATE",
        effective_at=effective_at,
        payload={
            "kind": "ORDER_STATE",
            "order_id": order.intent.order_id if isinstance(order, OrderState) else order.order_id,
            "from": from_status,
            "to": to_status,
            "reason": reason,
        },
        causation_id=order.intent.order_id if isinstance(order, OrderState) else order.order_id,
    )


def _commit_fill(
    config: EngineConfig,
    state: EngineState,
    event_input: CompletedBarEvent,
    correlation: str,
    bar: FeatureBar,
    *,
    order_id: str,
    order_side: str,
    effect: str,
    quantity: int,
    prospective: ProspectiveFill,
    causation_decision_id: str | None,
    reason: str,
) -> tuple[EngineState, Fill, FillEvidence]:
    sequence = state.next_fill_sequence
    pre_hash = _state_hash(state)
    anchor_selection = max(
        enumerate(bar.source_dataset_provenance),
        key=lambda pair: (
            next(
                selection.availability_at
                for selection in _current_bucket_selections(event_input, bar)
                if selection.bar.bar_record_id == pair[1].bar_record_id
            ),
            pair[0],
        ),
    )[1]
    fill_id = deterministic_uuid7(
        "fill",
        bar.start_at
        if reason.endswith("GAP") or reason in {"MARKET_ENTRY", "MARKET_CLOSE"}
        else bar.end_at,
        (
            config.run_id,
            config.lane_id,
            sequence,
            order_id,
            anchor_selection.bar_record_id,
            effect,
            reason,
            pre_hash,
        ),
    )
    fee = commission(config.cost_model.commission_per_contract_per_side, quantity)
    realized = Decimal("0.00")
    position = state.position
    cash = state.cash - fee
    fees = money_round(state.fees + fee)
    realized_total = state.realized_pnl
    position_after: PositionState | None = position
    signed_quantity = quantity if order_side == "buy" else -quantity
    average: Decimal | None = prospective.fill_price
    order_counter = state.next_order_sequence
    if effect == "open":
        side = "long" if order_side == "buy" else "short"
        stop_price, target_price = _entry_bracket_prices(
            config, state.pending_entry, prospective.fill_price, side
        )
        stop_id = deterministic_uuid7(
            "protective-stop-order",
            bar.start_at if reason.endswith("GAP") or reason == "MARKET_ENTRY" else bar.end_at,
            (
                config.run_id,
                config.lane_id,
                order_counter,
                order_id,
                fill_id,
                "sell" if side == "long" else "buy",
                quantity,
                stop_price,
            ),
        )
        target_id = deterministic_uuid7(
            "protective-target-order",
            bar.start_at if reason.endswith("GAP") or reason == "MARKET_ENTRY" else bar.end_at,
            (
                config.run_id,
                config.lane_id,
                order_counter + 1,
                order_id,
                fill_id,
                "sell" if side == "long" else "buy",
                quantity,
                target_price,
            ),
        )
        active_at = (
            bar.start_at if reason.endswith("GAP") or reason == "MARKET_ENTRY" else bar.end_at
        )
        bracket = ProtectiveBracketState(
            stop=ProtectiveOrderState(
                order_id=stop_id,
                role="protective_stop",
                order_type="stop_market",
                side="sell" if side == "long" else "buy",
                effect="close",
                quantity=quantity,
                trigger_price=stop_price,
                status="ACTIVE",
                active_from=active_at,
                filled_at=None,
                cancelled_at=None,
                status_reason="ORDER_ACTIVATED",
                entry_order_id=order_id,
                entry_fill_id=fill_id,
                order_sequence=order_counter,
                fill_sequence=None,
                originating_decision_id=cast(
                    str, cast(OrderState, state.pending_entry).originating_decision_id
                ),
            ),
            target=ProtectiveOrderState(
                order_id=target_id,
                role="profit_target",
                order_type="limit",
                side="sell" if side == "long" else "buy",
                effect="close",
                quantity=quantity,
                trigger_price=target_price,
                status="ACTIVE",
                active_from=active_at,
                filled_at=None,
                cancelled_at=None,
                status_reason="ORDER_ACTIVATED",
                entry_order_id=order_id,
                entry_fill_id=fill_id,
                order_sequence=order_counter + 1,
                fill_sequence=None,
                originating_decision_id=cast(
                    str, cast(OrderState, state.pending_entry).originating_decision_id
                ),
            ),
            both_hit_policy=config.fill_model.both_hit_policy,
            entry_bar_exit_policy=config.fill_model.entry_bar_exit_policy,
        )
        position_after = PositionState(
            side=cast(Any, side),
            quantity=quantity,
            contract_id=config.contract.contract_id,
            entry_order_id=order_id,
            entry_fill_id=fill_id,
            entry_price=prospective.fill_price,
            opened_at=active_at,
            opened_trading_day=_trading_day(config.calendar.windows, active_at) or active_at.date(),
            entry_commission=fee,
            protective_bracket=bracket,
        )
        order_counter += 2
        average = prospective.fill_price
    else:
        assert position is not None
        realized = price_pnl(
            side=position.side,
            entry=position.entry_price,
            exit_or_mark=prospective.fill_price,
            multiplier=config.contract.multiplier,
            quantity=quantity,
        )
        cash = money_round(state.cash + realized - fee)
        realized_total = money_round(state.realized_pnl + realized)
        position_after = None
        signed_quantity = 0
        average = None
    update: dict[str, Any] = {
        "cash": money_round(cash),
        "fees": fees,
        "realized_pnl": realized_total,
        "position": position_after,
        "next_order_sequence": order_counter,
        "next_fill_sequence": sequence + 1,
    }
    if effect == "open":
        risk = state.risk.model_copy(
            update={"filled_entries_in_trading_day": state.risk.filled_entries_in_trading_day + 1}
        )
        update["risk"] = risk
    state = state.model_copy(update=update)
    fill = Fill(
        schema_version="v1",
        fill_id=fill_id,
        owner_user_id=config.owner_user_id,
        run_id=config.run_id,
        lane_id=config.lane_id,
        order_id=order_id,
        contract_id=config.contract.contract_id,
        bar_record_id=anchor_selection.bar_record_id,
        fill_sequence=sequence,
        side=cast(Any, order_side),
        effect=cast(Any, effect),
        quantity=quantity,
        base_price=prospective.base_price,
        fill_price=prospective.fill_price,
        slippage=prospective.slippage,
        commission=fee,
        currency="USD",
        model_time=bar.start_at
        if reason.endswith("GAP") or reason in {"MARKET_ENTRY", "MARKET_CLOSE"}
        else bar.end_at,
        realized_pnl=realized,
        cash_after=state.cash,
        position_quantity_after=signed_quantity,
        position_average_after=average,
        reason=reason,
        causation_decision_id=causation_decision_id,
        created_at=event_input.emission_context.output_recorded_at,
        fencing_token=event_input.emission_context.fencing_token,
        record_version=1,
    )
    evidence = FillEvidence(
        fill_id=fill_id,
        fill_bar_start_at=bar.start_at,
        fill_bar_end_at=bar.end_at,
        bar_record_id_role="availability_anchor",
        availability_anchor_bar_record_id=anchor_selection.bar_record_id,
        source_bar_record_ids=bar.source_bar_record_ids,
        source_dataset_provenance=bar.source_dataset_provenance,
    )
    return state, fill, evidence


def _current_bucket_selections(
    event_input: CompletedBarEvent, bar: FeatureBar
) -> tuple[BarSelection, ...]:
    del event_input
    return bar.selections


def _entry_bracket_prices(
    config: EngineConfig, order: OrderState | None, entry: Decimal, side: str
) -> tuple[Decimal, Decimal]:
    if order is None:
        raise RuntimeError("entry order disappeared")
    intent = order.intent
    stop = intent.stop_price
    target = intent.target_price
    template = intent.bracket_template
    if stop is None:
        assert template is not None
        spec = template.stop
        if isinstance(spec, FixedTicksStop):
            stop = (
                entry - config.contract.tick_size * spec.ticks
                if side == "long"
                else entry + config.contract.tick_size * spec.ticks
            )
        elif isinstance(spec, AtrMultipleStop):
            value = template.frozen_feature_values[spec.feature_id].value
            assert isinstance(value, Decimal)
            distance = value * Decimal(spec.multiple)
            stop = entry - distance if side == "long" else entry + distance
        else:
            raise RuntimeError("setup stop must be frozen")
        stop = tick_round(
            stop, config.contract.tick_size, role="stop", side="sell" if side == "long" else "buy"
        )
    if target is None:
        assert template is not None
        target_spec = template.target
        if isinstance(target_spec, FixedTicksTarget):
            target = (
                entry + config.contract.tick_size * target_spec.ticks
                if side == "long"
                else entry - config.contract.tick_size * target_spec.ticks
            )
        elif isinstance(target_spec, RiskMultipleTarget):
            risk = abs(entry - stop)
            target = (
                entry + risk * Decimal(target_spec.multiple)
                if side == "long"
                else entry - risk * Decimal(target_spec.multiple)
            )
        else:
            raise RuntimeError("setup target must be frozen")
        target = tick_round(
            target,
            config.contract.tick_size,
            role="target",
            side="sell" if side == "long" else "buy",
        )
    return stop, target


def _process_fill_bar(
    config: EngineConfig,
    state: EngineState,
    event_input: CompletedBarEvent,
    correlation: str,
    bar: FeatureBar,
) -> tuple[
    EngineState,
    list[OrderIntent],
    list[ProtectiveOrderState],
    list[Fill],
    list[FillEvidence],
    list[RunEvent],
]:
    intents: list[OrderIntent] = []
    protective: list[ProtectiveOrderState] = []
    fills: list[Fill] = []
    evidence: list[FillEvidence] = []
    events: list[RunEvent] = []
    carried = state.position is not None
    # Activate eligible regular orders before opening-gap evaluation.
    for field in ("close_intent", "pending_entry"):
        order = cast(OrderState | None, getattr(state, field))
        if (
            order is not None
            and order.status == "PENDING"
            and order.intent.active_from <= bar.start_at
            and bar.start_at < order.intent.expires_at
        ):
            activated = order.model_copy(
                update={
                    "status": "ACTIVE",
                    "activated_at": bar.start_at,
                    "status_reason": "ORDER_ACTIVATED",
                }
            )
            state = state.model_copy(update={field: activated})
            state, emitted = _order_transition(
                config,
                state,
                event_input,
                correlation,
                activated,
                "PENDING",
                "ACTIVE",
                "ORDER_ACTIVATED",
                bar.start_at,
            )
            events.append(emitted)
    prices = BarPrices(bar.open, bar.high, bar.low, bar.close)
    if state.position is not None:
        pos = state.position
        bracket = pos.protective_bracket
        prospective: ProspectiveFill | None
        protective_candidate = protective_fill(
            position_side=pos.side,
            stop=bracket.stop.trigger_price,
            target=bracket.target.trigger_price,
            bar=prices,
            tick=config.contract.tick_size,
            stop_slippage_ticks=config.cost_model.stop_slippage_ticks,
            both_hit_policy=bracket.both_hit_policy,
        )
        close = state.close_intent
        if protective_candidate is not None and protective_candidate.reason.endswith("GAP"):
            prospective = protective_candidate
        elif close is not None and close.status == "ACTIVE":
            prospective = ProspectiveFill(
                bar.open,
                market_fill_price(
                    bar.open,
                    config.contract.tick_size,
                    config.cost_model.market_slippage_ticks,
                    close.intent.side,
                ),
                "MARKET_CLOSE",
            )
            order_id, side, cause = (
                close.intent.order_id,
                close.intent.side,
                close.originating_decision_id,
            )
        else:
            prospective = protective_candidate
        if prospective is not None and prospective is protective_candidate:
            leg = bracket.stop if "STOP" in prospective.reason else bracket.target
            order_id, side, cause = leg.order_id, leg.side, leg.originating_decision_id
        elif prospective is None:
            order_id = ""
            side = cast(Any, "")
            cause = None
        if prospective is not None:
            old_bracket = bracket
            close_before_fill = close
            state, fill, fill_ev = _commit_fill(
                config,
                state,
                event_input,
                correlation,
                bar,
                order_id=order_id,
                order_side=side,
                effect="close",
                quantity=pos.quantity,
                prospective=prospective,
                causation_decision_id=cause,
                reason=prospective.reason,
            )
            fills.append(fill)
            evidence.append(fill_ev)
            filled_leg: ProtectiveOrderState | None
            sibling: ProtectiveOrderState | None
            if order_id == old_bracket.stop.order_id:
                filled_leg, sibling = old_bracket.stop, old_bracket.target
            elif order_id == old_bracket.target.order_id:
                filled_leg, sibling = old_bracket.target, old_bracket.stop
            else:
                filled_leg = sibling = None
            if filled_leg is not None:
                assert sibling is not None
                filled_snapshot = filled_leg.model_copy(
                    update={
                        "status": "FILLED",
                        "filled_at": fill.model_time,
                        "status_reason": fill.reason,
                        "fill_sequence": fill.fill_sequence,
                    }
                )
                cancelled = sibling.model_copy(
                    update={
                        "status": "CANCELLED",
                        "cancelled_at": fill.model_time,
                        "status_reason": "POSITION_FLAT",
                    }
                )
                protective.extend(
                    [filled_snapshot, cancelled]
                    if filled_leg.role == "protective_stop"
                    else [cancelled, filled_snapshot]
                )
            else:
                protective.extend(
                    [
                        old_bracket.stop.model_copy(
                            update={
                                "status": "CANCELLED",
                                "cancelled_at": fill.model_time,
                                "status_reason": "POSITION_FLAT",
                            }
                        ),
                        old_bracket.target.model_copy(
                            update={
                                "status": "CANCELLED",
                                "cancelled_at": fill.model_time,
                                "status_reason": "POSITION_FLAT",
                            }
                        ),
                    ]
                )
            post_close_update: dict[str, Any] = {"close_intent": None, "status": "ACTIVE"}
            if (
                close_before_fill is not None
                and close_before_fill.creation_cause == "CONTRACT_LIQUIDATION"
            ):
                post_close_update.update(status="STOPPED", finished_reason="CONTRACT_CLOSED")
            elif (
                close_before_fill is not None and close_before_fill.creation_cause == "FORCE_CLOSE"
            ):
                post_close_update.update(force_close_state="completed")
            state = state.model_copy(update=post_close_update)
            if filled_leg is not None:
                state, emitted = _order_transition(
                    config,
                    state,
                    event_input,
                    correlation,
                    filled_snapshot,
                    "ACTIVE",
                    "FILLED",
                    fill.reason,
                    fill.model_time,
                )
                events.append(emitted)
            elif close_before_fill is not None:
                filled_close = close_before_fill.model_copy(
                    update={
                        "status": "FILLED",
                        "status_reason": fill.reason,
                        "fill_sequence": fill.fill_sequence,
                    }
                )
                state, emitted = _order_transition(
                    config,
                    state,
                    event_input,
                    correlation,
                    filled_close,
                    "ACTIVE",
                    "FILLED",
                    fill.reason,
                    fill.model_time,
                )
                events.append(emitted)
            state, emitted = _emit_event(
                config,
                state,
                event_input,
                correlation,
                event_type="FILL_RECORDED",
                effective_at=fill.model_time,
                payload={
                    "kind": "FILL_RECORDED",
                    "fill_id": fill.fill_id,
                    "order_id": fill.order_id,
                    "fill_sequence": fill.fill_sequence,
                    "evidence_mode": "historical"
                    if config.mode == "backtest"
                    else "contemporaneous",
                },
                causation_id=fill.fill_id,
            )
            events.append(emitted)
            cancellations = (
                (sibling,) if filled_leg is not None and sibling is not None else old_bracket
            )
            for cancelled_leg in (
                cancellations
                if isinstance(cancellations, tuple)
                else (cancellations.stop, cancellations.target)
            ):
                cancelled_snapshot = cancelled_leg.model_copy(
                    update={
                        "status": "CANCELLED",
                        "cancelled_at": fill.model_time,
                        "status_reason": "POSITION_FLAT",
                    }
                )
                state, emitted = _order_transition(
                    config,
                    state,
                    event_input,
                    correlation,
                    cancelled_snapshot,
                    "ACTIVE",
                    "CANCELLED",
                    "POSITION_FLAT",
                    fill.model_time,
                )
                events.append(emitted)
            scheduled = state.scheduled_force_close
            if scheduled is not None:
                cancelled_force = scheduled.model_copy(
                    update={"status": "CANCELLED", "status_reason": "POSITION_FLAT"}
                )
                state = state.model_copy(
                    update={"scheduled_force_close": None, "force_close_state": "waiting"}
                )
                state, emitted = _order_transition(
                    config,
                    state,
                    event_input,
                    correlation,
                    cancelled_force,
                    scheduled.status,
                    "CANCELLED",
                    "POSITION_FLAT",
                    fill.model_time,
                )
                events.append(emitted)
    if state.position is None and state.pending_entry is not None:
        order = state.pending_entry
        if bar.start_at >= order.intent.expires_at:
            expired = order.model_copy(
                update={"status": "EXPIRED", "status_reason": "ORDER_EXPIRED"}
            )
            state = state.model_copy(update={"pending_entry": None})
            state, emitted = _order_transition(
                config,
                state,
                event_input,
                correlation,
                expired,
                order.status,
                "EXPIRED",
                "ORDER_EXPIRED",
                order.intent.expires_at,
            )
            events.append(emitted)
        elif bar.start_at >= order.intent.active_from and not _entry_window_allows(
            config, bar.start_at
        ):
            cancelled_entry = order.model_copy(
                update={"status": "CANCELLED", "status_reason": "ENTRY_WINDOW_CLOSED"}
            )
            state = state.model_copy(update={"pending_entry": None})
            state, emitted = _order_transition(
                config,
                state,
                event_input,
                correlation,
                cancelled_entry,
                order.status,
                "CANCELLED",
                "ENTRY_WINDOW_CLOSED",
                bar.start_at,
            )
            events.append(emitted)
        elif order.status == "ACTIVE" and _entry_window_allows(config, bar.start_at):
            if order.intent.order_type == "market":
                prospective = ProspectiveFill(
                    bar.open,
                    market_fill_price(
                        bar.open,
                        config.contract.tick_size,
                        config.cost_model.market_slippage_ticks,
                        order.intent.side,
                    ),
                    "MARKET_ENTRY",
                )
            else:
                selected = limit_fill(
                    side=order.intent.side,
                    limit=cast(Decimal, order.intent.executable_price),
                    open_price=bar.open,
                    high=bar.high,
                    low=bar.low,
                    tick=config.contract.tick_size,
                    slippage_ticks=config.cost_model.limit_slippage_ticks,
                )
                prospective = ProspectiveFill(*selected) if selected is not None else None
                window_close = _first_entry_window_close(config, bar.start_at, bar.end_at)
                if window_close is not None and (
                    prospective is None or prospective.reason != "LIMIT_ENTRY_GAP"
                ):
                    cancelled_entry_window = order.model_copy(
                        update={
                            "status": "CANCELLED",
                            "status_reason": "ENTRY_WINDOW_CLOSED",
                        }
                    )
                    state = state.model_copy(update={"pending_entry": None})
                    state, emitted = _order_transition(
                        config,
                        state,
                        event_input,
                        correlation,
                        cancelled_entry_window,
                        order.status,
                        "CANCELLED",
                        "ENTRY_WINDOW_CLOSED",
                        window_close,
                    )
                    events.append(emitted)
                    prospective = None
            if prospective is not None:
                side_name = "long" if order.intent.side == "buy" else "short"
                stop, target = _entry_bracket_prices(
                    config, order, prospective.fill_price, side_name
                )
                slipped_stop = (
                    stop - config.contract.tick_size * config.cost_model.stop_slippage_ticks
                    if side_name == "long"
                    else stop + config.contract.tick_size * config.cost_model.stop_slippage_ticks
                )
                geometry = (
                    stop < prospective.fill_price < target
                    if side_name == "long"
                    else target < prospective.fill_price < stop
                )
                risk = modeled_total_loss(
                    entry=prospective.fill_price,
                    slipped_stop=slipped_stop,
                    multiplier=config.contract.multiplier,
                    quantity=order.intent.quantity,
                    commission_rate=config.cost_model.commission_per_contract_per_side,
                )
                cap = config.risk_policy.per_entry_loss_cap
                if config.sizing_policy.kind == "stop_risk_fraction":
                    cap = min(
                        cap, config.sizing_policy.fraction * min(config.starting_cash, state.equity)
                    )
                if not geometry or risk > cap:
                    reason = "ENTRY_GEOMETRY_GAP" if not geometry else "RISK_GAP"
                    cancelled_order = order.model_copy(
                        update={"status": "CANCELLED", "status_reason": reason}
                    )
                    state = state.model_copy(update={"pending_entry": None})
                    state, emitted = _order_transition(
                        config,
                        state,
                        event_input,
                        correlation,
                        cancelled_order,
                        order.status,
                        "CANCELLED",
                        reason,
                        prospective and bar.start_at or bar.end_at,
                    )
                    events.append(emitted)
                else:
                    state, fill, fill_ev = _commit_fill(
                        config,
                        state,
                        event_input,
                        correlation,
                        bar,
                        order_id=order.intent.order_id,
                        order_side=order.intent.side,
                        effect="open",
                        quantity=order.intent.quantity,
                        prospective=prospective,
                        causation_decision_id=order.originating_decision_id,
                        reason=prospective.reason,
                    )
                    filled_order = order.model_copy(
                        update={
                            "status": "FILLED",
                            "status_reason": fill.reason,
                            "fill_sequence": fill.fill_sequence,
                        }
                    )
                    state = state.model_copy(update={"pending_entry": None})
                    fills.append(fill)
                    evidence.append(fill_ev)
                    bracket = cast(PositionState, state.position).protective_bracket
                    protective.extend((bracket.stop, bracket.target))
                    state, emitted = _order_transition(
                        config,
                        state,
                        event_input,
                        correlation,
                        filled_order,
                        order.status,
                        "FILLED",
                        fill.reason,
                        fill.model_time,
                    )
                    events.append(emitted)
                    state, emitted = _emit_event(
                        config,
                        state,
                        event_input,
                        correlation,
                        event_type="FILL_RECORDED",
                        effective_at=fill.model_time,
                        payload={
                            "kind": "FILL_RECORDED",
                            "fill_id": fill.fill_id,
                            "order_id": fill.order_id,
                            "fill_sequence": fill.fill_sequence,
                            "evidence_mode": "historical"
                            if config.mode == "backtest"
                            else "contemporaneous",
                        },
                        causation_id=fill.fill_id,
                    )
                    events.append(emitted)
                    for leg in (bracket.stop, bracket.target):
                        state, emitted = _order_transition(
                            config,
                            state,
                            event_input,
                            correlation,
                            leg,
                            "NOT_CREATED",
                            "ACTIVE",
                            "ORDER_ACTIVATED",
                            fill.model_time,
                        )
                        events.append(emitted)
                    if config.mode == "backtest" and config.force_close_at is not None:
                        force_intent = _close_intent(
                            config,
                            state,
                            event_input,
                            None,
                            config.force_close_at,
                            "FORCE_CLOSE",
                            expires_at=cast(datetime, config.end_at),
                        )
                        scheduled = OrderState(
                            intent=force_intent,
                            order_sequence=state.next_order_sequence,
                            status="PENDING",
                            status_reason="ORDER_SUBMITTED",
                            activated_at=None,
                            fill_sequence=None,
                            source_setup_id=None,
                            originating_decision_id=None,
                            creation_cause="FORCE_CLOSE",
                        )
                        state = state.model_copy(
                            update={
                                "scheduled_force_close": scheduled,
                                "next_order_sequence": state.next_order_sequence + 1,
                            }
                        )
                        intents.append(force_intent)
                        state, emitted = _order_transition(
                            config,
                            state,
                            event_input,
                            correlation,
                            scheduled,
                            "NOT_CREATED",
                            "PENDING",
                            "ORDER_SUBMITTED",
                            force_intent.submitted_at,
                        )
                        events.append(emitted)
                    position = cast(PositionState, state.position)
                    same_bar: ProspectiveFill | None = None
                    if prospective.reason in {"MARKET_ENTRY", "LIMIT_ENTRY_GAP"}:
                        same_bar = protective_fill(
                            position_side=position.side,
                            stop=bracket.stop.trigger_price,
                            target=bracket.target.trigger_price,
                            bar=BarPrices(bar.open, bar.high, bar.low, bar.close),
                            tick=config.contract.tick_size,
                            stop_slippage_ticks=config.cost_model.stop_slippage_ticks,
                            both_hit_policy=config.fill_model.both_hit_policy,
                        )
                    elif config.fill_model.entry_bar_exit_policy == "conservative_stop_first":
                        stop_hit = (
                            bar.low <= bracket.stop.trigger_price
                            if position.side == "long"
                            else bar.high >= bracket.stop.trigger_price
                        )
                        if stop_hit:
                            base = bracket.stop.trigger_price
                            slipped = (
                                base
                                - config.contract.tick_size * config.cost_model.stop_slippage_ticks
                                if position.side == "long"
                                else base
                                + config.contract.tick_size * config.cost_model.stop_slippage_ticks
                            )
                            same_bar = ProspectiveFill(base, slipped, "ENTRY_BAR_CONSERVATIVE_STOP")
                    if same_bar is not None:
                        state, exit_fill, exit_evidence = _commit_fill(
                            config,
                            state,
                            event_input,
                            correlation,
                            bar,
                            order_id=bracket.stop.order_id
                            if "STOP" in same_bar.reason
                            else bracket.target.order_id,
                            order_side=bracket.stop.side
                            if "STOP" in same_bar.reason
                            else bracket.target.side,
                            effect="close",
                            quantity=position.quantity,
                            prospective=same_bar,
                            causation_decision_id=bracket.stop.originating_decision_id,
                            reason=same_bar.reason,
                        )
                        fills.append(exit_fill)
                        evidence.append(exit_evidence)
                        filled_leg = bracket.stop if "STOP" in same_bar.reason else bracket.target
                        sibling = bracket.target if filled_leg is bracket.stop else bracket.stop
                        filled_snapshot = filled_leg.model_copy(
                            update={
                                "status": "FILLED",
                                "filled_at": exit_fill.model_time,
                                "status_reason": exit_fill.reason,
                                "fill_sequence": exit_fill.fill_sequence,
                            }
                        )
                        cancelled_snapshot = sibling.model_copy(
                            update={
                                "status": "CANCELLED",
                                "cancelled_at": exit_fill.model_time,
                                "status_reason": "POSITION_FLAT",
                            }
                        )
                        protective.extend(
                            (filled_snapshot, cancelled_snapshot)
                            if filled_leg.role == "protective_stop"
                            else (cancelled_snapshot, filled_snapshot)
                        )
                        state, emitted = _order_transition(
                            config,
                            state,
                            event_input,
                            correlation,
                            filled_snapshot,
                            "ACTIVE",
                            "FILLED",
                            exit_fill.reason,
                            exit_fill.model_time,
                        )
                        events.append(emitted)
                        state, emitted = _emit_event(
                            config,
                            state,
                            event_input,
                            correlation,
                            event_type="FILL_RECORDED",
                            effective_at=exit_fill.model_time,
                            payload={
                                "kind": "FILL_RECORDED",
                                "fill_id": exit_fill.fill_id,
                                "order_id": exit_fill.order_id,
                                "fill_sequence": exit_fill.fill_sequence,
                                "evidence_mode": "historical"
                                if config.mode == "backtest"
                                else "contemporaneous",
                            },
                            causation_id=exit_fill.fill_id,
                        )
                        events.append(emitted)
                        state, emitted = _order_transition(
                            config,
                            state,
                            event_input,
                            correlation,
                            cancelled_snapshot,
                            "ACTIVE",
                            "CANCELLED",
                            "POSITION_FLAT",
                            exit_fill.model_time,
                        )
                        events.append(emitted)
                        scheduled = state.scheduled_force_close
                        if scheduled is not None:
                            cancelled_force = scheduled.model_copy(
                                update={
                                    "status": "CANCELLED",
                                    "status_reason": "POSITION_FLAT",
                                }
                            )
                            state = state.model_copy(
                                update={
                                    "scheduled_force_close": None,
                                    "force_close_state": "waiting",
                                }
                            )
                            state, emitted = _order_transition(
                                config,
                                state,
                                event_input,
                                correlation,
                                cancelled_force,
                                "PENDING",
                                "CANCELLED",
                                "POSITION_FLAT",
                                exit_fill.model_time,
                            )
                            events.append(emitted)
    # Exposure is accrued only for a complete eligible fill bar.
    if bar.end_at > config.start_at and (config.end_at is None or bar.start_at < config.end_at):
        duration = int((bar.end_at - bar.start_at).total_seconds())
        if carried:
            delta = (
                duration
                if state.position is not None
                or any(fill.effect == "close" and fill.model_time == bar.end_at for fill in fills)
                else 0
            )
        elif state.position is not None or fills:
            delta = (
                duration
                if any(fill.effect == "open" and fill.model_time == bar.start_at for fill in fills)
                else 0
            )
        else:
            delta = 0
        state = state.model_copy(update={"exposure_seconds": state.exposure_seconds + delta})
    return state, intents, protective, fills, evidence, events


def _mark(
    config: EngineConfig,
    state: EngineState,
    event_input: CompletedBarEvent,
    correlation: str,
    bar: FeatureBar,
) -> tuple[EngineState, tuple[RunEvent, ...], MarkEvidence]:
    unrealized = Decimal("0.00")
    if state.position is not None:
        unrealized = price_pnl(
            side=state.position.side,
            entry=state.position.entry_price,
            exit_or_mark=bar.close,
            multiplier=config.contract.multiplier,
            quantity=state.position.quantity,
        )
    equity = money_round(state.cash + unrealized)
    high = max(state.risk.high_water, equity)
    drawdown = money_round(max(Decimal(0), high - equity))
    trading_day = _trading_day(config.calendar.windows, bar.start_at)
    risk = state.risk
    if trading_day != risk.trading_day:
        risk = risk.model_copy(
            update={
                "trading_day": trading_day,
                "daily_start_equity": state.equity,
                "filled_entries_in_trading_day": 0,
                "latches": tuple(x for x in risk.latches if x == "CUMULATIVE_DRAWDOWN_LIMIT"),
            }
        )
    daily_loss = max(Decimal(0), risk.daily_start_equity - equity)
    newly_latched: list[str] = []
    if daily_loss >= config.risk_policy.daily_loss_cap and "DAILY_LOSS_LIMIT" not in risk.latches:
        newly_latched.append("DAILY_LOSS_LIMIT")
    if (
        drawdown >= config.risk_policy.cumulative_drawdown_cap
        and "CUMULATIVE_DRAWDOWN_LIMIT" not in risk.latches
    ):
        newly_latched.append("CUMULATIVE_DRAWDOWN_LIMIT")
    risk = risk.model_copy(
        update={
            "high_water": high,
            "current_drawdown": drawdown,
            "maximum_drawdown": max(risk.maximum_drawdown, drawdown),
        }
    )
    state = state.model_copy(
        update={
            "equity": equity,
            "unrealized_pnl": unrealized,
            "last_mark": bar.close,
            "last_mark_bar_record_id": bar.source_bar_record_ids[-1],
            "last_mark_source_bar_record_ids": bar.source_bar_record_ids,
            "last_mark_source_dataset_provenance": bar.source_dataset_provenance,
            "mark_status": "fresh",
            "risk": risk,
        }
    )
    state, mark_event = _emit_event(
        config,
        state,
        event_input,
        correlation,
        event_type="MARK_RECORDED",
        effective_at=bar.end_at,
        payload={
            "kind": "MARK_RECORDED",
            "bar_record_id": bar.source_bar_record_ids[-1],
            "price": bar.close,
            "status": "fresh",
            "equity": equity,
            "drawdown": drawdown,
        },
        causation_id=None,
    )
    events = [mark_event]
    if newly_latched:
        pending = state.pending_entry
        canonical_latches = tuple(
            item
            for item in ("DAILY_LOSS_LIMIT", "CUMULATIVE_DRAWDOWN_LIMIT")
            if item in {*risk.latches, *newly_latched}
        )
        cancelled_setups = tuple(
            setup.model_copy(update={"status": "CANCELLED", "reason_code": newly_latched[0]})
            if setup.status in {"ARMED", "ENTRY_PENDING"}
            else setup
            for setup in state.setups
        )
        state = state.model_copy(
            update={
                "risk": risk.model_copy(update={"latches": canonical_latches}),
                "pending_entry": None,
                "setups": cancelled_setups,
            }
        )
        for latch in newly_latched:
            observed = daily_loss if latch == "DAILY_LOSS_LIMIT" else drawdown
            limit = (
                config.risk_policy.daily_loss_cap
                if latch == "DAILY_LOSS_LIMIT"
                else config.risk_policy.cumulative_drawdown_cap
            )
            state, emitted = _emit_event(
                config,
                state,
                event_input,
                correlation,
                event_type="RISK_LATCHED",
                effective_at=bar.end_at,
                payload={
                    "kind": "RISK_LATCHED",
                    "reason": latch,
                    "observed": observed,
                    "limit": limit,
                    "trading_day": risk.trading_day,
                },
                causation_id=None,
            )
            events.append(emitted)
        if pending is not None:
            cancelled = pending.model_copy(
                update={"status": "CANCELLED", "status_reason": newly_latched[0]}
            )
            state, emitted = _order_transition(
                config,
                state,
                event_input,
                correlation,
                cancelled,
                pending.status,
                "CANCELLED",
                newly_latched[0],
                bar.end_at,
            )
            events.append(emitted)
    return (
        state,
        tuple(events),
        MarkEvidence(
            run_event_id=mark_event.run_event_id,
            fill_bar_start_at=bar.start_at,
            fill_bar_end_at=bar.end_at,
            close_bar_record_id=bar.source_bar_record_ids[-1],
            source_bar_record_ids=bar.source_bar_record_ids,
            source_dataset_provenance=bar.source_dataset_provenance,
        ),
    )


def _create_decision(
    config: EngineConfig,
    state: EngineState,
    event_input: CompletedBarEvent,
    correlation: str,
    bar: FeatureBar,
) -> tuple[EngineState, Decision, OrderIntent | None]:
    definition = config.strategy_version.definition
    histories = {runtime.feature_id: runtime.history for runtime in state.feature_runtime}
    if state.position is not None:
        side = state.position.side
        root = (
            definition.exit_rules.long_root if side == "long" else definition.exit_rules.short_root
        )
        status, results = evaluate_rule(
            definition, root, histories, bar.known_at, context="exit_rule"
        )
        decision_type, reason = (
            ("CLOSE", "EXIT_RULE_PASS")
            if status == "PASS"
            else (
                "HOLD",
                next((r.reason_code for r in results if r.result == "UNKNOWN"), "EXIT_RULE_FAIL"),
            )
        )
        stage = "exit_rule"
    else:
        evaluations = []
        for side, root in (
            ("long", definition.entry_rules.long_root),
            ("short", definition.entry_rules.short_root),
        ):
            if root is not None:
                status, results = evaluate_rule(
                    definition, root, histories, bar.known_at, context="entry_rule"
                )
                evaluations.append((side, status, results))
        passed = [item for item in evaluations if item[1] == "PASS"]
        if len(passed) > 1:
            side, results, decision_type, reason = (
                None,
                tuple(x for item in evaluations for x in item[2]),
                "REJECT",
                "CONFLICT_OPPOSING_SIDES",
            )
        elif passed:
            side, _, results = passed[0]
            decision_type, reason = "ENTRY", "ENTRY_RULE_PASS"
        else:
            side = evaluations[0][0] if len(evaluations) == 1 else None
            results = tuple(x for item in evaluations for x in item[2])
            unknown = next((r.reason_code for r in results if r.result == "UNKNOWN"), None)
            decision_type, reason = "HOLD", unknown or "ENTRY_RULE_FAIL"
        if state.pending_entry is not None and decision_type == "ENTRY":
            decision_type, reason = "HOLD", "ENTRY_PENDING"
        stage = "entry_rule"
    sources = tuple(
        dict.fromkeys(
            (
                *tuple(source for result in results for source in result.source_bar_record_ids),
                *bar.source_bar_record_ids,
            )
        )
    )
    pre_hash = _state_hash(state)
    sequence = state.next_decision_sequence
    effective_at = (
        bar.end_at
        if config.mode == "backtest"
        else max((bar.known_at, *(item.known_at for item in results)))
    )
    decision_id = deterministic_uuid7(
        "decision",
        effective_at,
        (
            config.run_id,
            config.lane_id,
            sequence,
            config.strategy_version.strategy_version_id,
            bar.end_at,
            decision_type,
            side,
            None,
            pre_hash,
            correlation,
        ),
    )
    intent: OrderIntent | None = None
    update: dict[str, Any] = {"next_decision_sequence": sequence + 1}
    if decision_type == "ENTRY" and side is not None:
        intent, reason = _entry_intent(
            config, state, event_input, decision_id, side, effective_at, bar
        )
        decision_type = "ENTRY" if intent is not None else "REJECT"
        if intent is not None:
            order_state = OrderState(
                intent=intent,
                order_sequence=state.next_order_sequence,
                status="ACTIVE"
                if intent.active_from <= event_input.emission_context.order_submitted_at
                else "PENDING",
                status_reason="ORDER_SUBMITTED",
                activated_at=intent.active_from
                if intent.active_from <= event_input.emission_context.order_submitted_at
                else None,
                fill_sequence=None,
                source_setup_id=None,
                originating_decision_id=decision_id,
                creation_cause="ENTRY",
            )
            update.update(
                pending_entry=order_state, next_order_sequence=state.next_order_sequence + 1
            )
    elif decision_type == "CLOSE" and state.position is not None and state.close_intent is None:
        intent = _close_intent(config, state, event_input, decision_id, effective_at, "EXIT_RULE")
        order_state = OrderState(
            intent=intent,
            order_sequence=state.next_order_sequence,
            status="PENDING",
            status_reason="ORDER_SUBMITTED",
            activated_at=None,
            fill_sequence=None,
            source_setup_id=None,
            originating_decision_id=decision_id,
            creation_cause="EXIT_RULE",
        )
        update.update(
            close_intent=order_state,
            status="CLOSING",
            next_order_sequence=state.next_order_sequence + 1,
        )
    elif decision_type == "CLOSE":
        decision_type, reason = "HOLD", "CLOSE_PENDING"
    state = state.model_copy(update=update)
    post_hash = _state_hash(state)
    idempotency = deterministic_uuid7(
        "decision-idempotency",
        effective_at,
        (
            config.run_id,
            config.lane_id,
            sequence,
            config.strategy_version.strategy_version_id,
            bar.end_at,
            decision_type,
            side,
            None,
            pre_hash,
            correlation,
            decision_id,
        ),
    )
    dataset_id = _decision_dataset(config, sources, state, bar)
    decided_at = bar.end_at if config.mode == "backtest" else event_input.recorded_at
    decision = Decision(
        schema_version="v1",
        decision_id=decision_id,
        owner_user_id=config.owner_user_id,
        run_id=config.run_id,
        lane_id=config.lane_id,
        strategy_version_id=config.strategy_version.strategy_version_id,
        dataset_revision_id=dataset_id,
        source_bar_record_ids=sources,
        execution_bar_end=bar.end_at,
        decision_sequence=sequence,
        decision_type=cast(Any, decision_type),
        side=cast(Any, side),
        setup_id=None,
        reason_code=reason,
        evidence=DecisionEvidence(
            evaluation_stage=cast(Any, stage),
            evidence_mode="historical" if config.mode == "backtest" else "contemporaneous",
            results=tuple(sorted(results, key=lambda item: item.node_id)),
            selected_setup=None,
        ),
        order_intent=intent,
        pre_state_sha256=pre_hash,
        post_state_sha256=post_hash,
        causation_event_id=correlation,
        effective_at=effective_at,
        decided_at=decided_at,
        idempotency_key=idempotency,
        created_at=decided_at,
        record_version=1,
    )
    return state, decision, intent


def _decision_dataset(
    config: EngineConfig, sources: tuple[str, ...], state: EngineState, bar: FeatureBar
) -> str | None:
    if config.mode == "backtest":
        assert config.dataset_revision is not None
        return config.dataset_revision.dataset_revision_id
    mapping = {
        item.bar_record_id: item.published_base_revision_id
        for item in bar.source_dataset_provenance
    }
    for runtime in state.feature_runtime:
        for value in runtime.history:
            mapping.update(
                (item.bar_record_id, item.published_base_revision_id)
                for item in value.source_dataset_provenance
            )
    values = {mapping.get(source) for source in sources}
    selected = next(iter(values)) if len(values) == 1 and None not in values else None
    return selected


def _next_fill_start(config: EngineConfig, earliest: datetime) -> datetime:
    interval = config.fill_model.fill_interval_seconds
    for window in config.calendar.windows:
        if window.kind != "open" or window.end_at <= earliest:
            continue
        candidate = max(earliest, window.start_at)
        elapsed = max(0, int((candidate - window.start_at).total_seconds()))
        offset = (elapsed // interval) * interval
        value = window.start_at + timedelta(seconds=offset)
        if value < candidate:
            value += timedelta(seconds=interval)
        while value < window.end_at:
            if _entry_window_allows(config, value):
                return value
            value += timedelta(seconds=interval)
    return earliest


def _entry_intent(
    config: EngineConfig,
    state: EngineState,
    event_input: CompletedBarEvent,
    decision_id: str,
    side: str,
    effective_at: datetime,
    bar: FeatureBar,
) -> tuple[OrderIntent | None, str]:
    definition = config.strategy_version.definition
    policy = definition.order_policy
    order_side = "buy" if side == "long" else "sell"
    reference = (
        bar.close + config.contract.tick_size * config.cost_model.market_slippage_ticks
        if side == "long"
        else bar.close - config.contract.tick_size * config.cost_model.market_slippage_ticks
    )
    features = {value.feature_id: value for value in state.feature_values}
    raw_price = executable = None
    if policy.entry_type == "limit":
        source = policy.limit_price_source
        if (
            source is None
            or source.kind != "feature"
            or source.feature_id not in features
            or not isinstance(features[source.feature_id].value, Decimal)
        ):
            return None, "INVALID_BRACKET"
        raw_price = (
            cast(Decimal, features[source.feature_id].value)
            + source.offset_ticks * config.contract.tick_size
        )
        executable = tick_round(raw_price, config.contract.tick_size, role="entry", side=order_side)
        reference = executable
    stop_spec, target_spec = definition.exit_policy.stop, definition.exit_policy.target
    stop: Decimal | None = None
    target: Decimal | None = None
    frozen: dict[str, Any] = {}
    if isinstance(stop_spec, FixedTicksStop):
        stop = (
            reference - stop_spec.ticks * config.contract.tick_size
            if side == "long"
            else reference + stop_spec.ticks * config.contract.tick_size
        )
    elif isinstance(stop_spec, AtrMultipleStop):
        value = features.get(stop_spec.feature_id)
        if value is None or not isinstance(value.value, Decimal):
            return None, "RISK_SIZE_ZERO"
        frozen[value.feature_id] = value
        distance = value.value * Decimal(stop_spec.multiple)
        stop = reference - distance if side == "long" else reference + distance
    if stop is not None:
        stop = tick_round(
            stop, config.contract.tick_size, role="stop", side="sell" if side == "long" else "buy"
        )
    if isinstance(target_spec, FixedTicksTarget):
        target = (
            reference + target_spec.ticks * config.contract.tick_size
            if side == "long"
            else reference - target_spec.ticks * config.contract.tick_size
        )
    elif isinstance(target_spec, RiskMultipleTarget) and stop is not None:
        distance = abs(reference - stop) * Decimal(target_spec.multiple)
        target = reference + distance if side == "long" else reference - distance
    if target is not None:
        target = tick_round(
            target,
            config.contract.tick_size,
            role="target",
            side="sell" if side == "long" else "buy",
        )
    if (
        stop is None
        or target is None
        or not (stop < reference < target if side == "long" else target < reference < stop)
    ):
        return None, "INVALID_BRACKET"
    slipped_stop = (
        stop - config.contract.tick_size * config.cost_model.stop_slippage_ticks
        if side == "long"
        else stop + config.contract.tick_size * config.cost_model.stop_slippage_ticks
    )
    if config.sizing_policy.kind == "fixed_contracts":
        quantity = config.sizing_policy.quantity
        if (
            modeled_total_loss(
                entry=reference,
                slipped_stop=slipped_stop,
                multiplier=config.contract.multiplier,
                quantity=quantity,
                commission_rate=config.cost_model.commission_per_contract_per_side,
            )
            > config.risk_policy.per_entry_loss_cap
        ):
            return None, "RISK_SIZE_ZERO"
    else:
        quantity = stop_fraction_quantity(
            starting_cash=config.starting_cash,
            current_equity=state.equity,
            fraction=config.sizing_policy.fraction,
            max_quantity=config.sizing_policy.max_quantity,
            per_entry_cap=config.risk_policy.per_entry_loss_cap,
            entry=reference,
            slipped_stop=slipped_stop,
            multiplier=config.contract.multiplier,
            commission_rate=config.cost_model.commission_per_contract_per_side,
        )
        if quantity == 0:
            return None, "RISK_SIZE_ZERO"
    if state.risk.latches:
        return None, state.risk.latches[0]
    effective_cap = min(
        definition.constraints.max_entries_per_trading_day,
        config.risk_policy.max_entries_per_trading_day,
    )
    if state.risk.filled_entries_in_trading_day >= effective_cap:
        return None, "DAILY_ENTRY_LIMIT"
    submitted = event_input.emission_context.order_submitted_at
    active = _next_fill_start(config, max(effective_at, submitted))
    expires = entry_expiry(
        bar.end_at,
        submitted,
        config.strategy_version.execution_interval_seconds,
        policy.entry_ttl_execution_bars,
    )
    if effective_at >= config.contract.entry_cutoff_at:
        return None, "ENTRY_CUTOFF"
    if not _entry_window_allows(config, active):
        return None, "ENTRY_WINDOW_CLOSED"
    if config.force_close_at is not None and expires > config.force_close_at:
        return None, "FORCE_CLOSE"
    sequence = state.next_order_sequence
    order_id = deterministic_uuid7(
        "entry-order",
        effective_at,
        (
            config.run_id,
            config.lane_id,
            sequence,
            decision_id,
            None,
            side,
            policy.entry_type,
            quantity,
        ),
    )
    from familytrade.simulation.state import BracketTemplate

    intent = OrderIntent(
        order_id=order_id,
        kind="entry",
        order_type=policy.entry_type,
        side=cast(Any, order_side),
        effect="open",
        quantity=quantity,
        raw_price=raw_price,
        executable_price=executable,
        raw_stop=stop if raw_price is not None else None,
        stop_price=stop if raw_price is not None else None,
        raw_target=target if raw_price is not None else None,
        target_price=target if raw_price is not None else None,
        bracket_template=BracketTemplate(
            stop=stop_spec, target=target_spec, frozen_feature_values=frozen
        ),
        effective_at=effective_at,
        submitted_at=submitted,
        active_from=active,
        expires_at=expires,
        entry_bar_exit_policy=config.fill_model.entry_bar_exit_policy,
        both_hit_policy=config.fill_model.both_hit_policy,
    )
    return intent, "ENTRY_RULE_PASS"


def _close_intent(
    config: EngineConfig,
    state: EngineState,
    event_input: CompletedBarEvent | DataQualityEvent | FinishRunEvent,
    decision_id: str | None,
    effective_at: datetime,
    creation_cause: str,
    *,
    expires_at: datetime | None = None,
) -> OrderIntent:
    position = cast(PositionState, state.position)
    side = "sell" if position.side == "long" else "buy"
    sequence = state.next_order_sequence
    order_id = deterministic_uuid7(
        "close-order",
        effective_at,
        (
            config.run_id,
            config.lane_id,
            sequence,
            decision_id,
            position.entry_fill_id,
            side,
            position.quantity,
            creation_cause,
        ),
    )
    submitted = event_input.emission_context.order_submitted_at
    return OrderIntent(
        order_id=order_id,
        kind="close",
        order_type="market",
        side=cast(Any, side),
        effect="close",
        quantity=position.quantity,
        raw_price=None,
        executable_price=None,
        raw_stop=None,
        stop_price=None,
        raw_target=None,
        target_price=None,
        bracket_template=None,
        effective_at=effective_at,
        submitted_at=submitted,
        active_from=_next_fill_start(config, max(effective_at, submitted)),
        expires_at=expires_at
        or (
            config.contract.liquidation_start_at
            if creation_cause == "EXIT_RULE"
            else config.contract.last_trade_at
        ),
        entry_bar_exit_policy=config.fill_model.entry_bar_exit_policy,
        both_hit_policy=config.fill_model.both_hit_policy,
    )


def _lifecycle_boundary(
    config: EngineConfig,
    state: EngineState,
    event_input: CompletedBarEvent | DataQualityEvent | FinishRunEvent,
    correlation: str,
    boundary: datetime,
) -> tuple[EngineState, tuple[OrderIntent, ...], tuple[RunEvent, ...]]:
    """Apply policy transitions exposed by the next accepted chronology boundary."""
    intents: list[OrderIntent] = []
    events: list[RunEvent] = []

    def cancel_entry(reason: str) -> None:
        nonlocal state
        order = state.pending_entry
        if order is None:
            return
        cancelled = order.model_copy(update={"status": "CANCELLED", "status_reason": reason})
        state = state.model_copy(update={"pending_entry": None})
        state, emitted = _order_transition(
            config,
            state,
            event_input,
            correlation,
            cancelled,
            order.status,
            "CANCELLED",
            reason,
            boundary,
        )
        events.append(emitted)

    if boundary >= config.contract.entry_cutoff_at:
        cancel_entry("ENTRY_CUTOFF")

    if (
        config.mode == "backtest"
        and config.force_close_at is not None
        and boundary >= config.force_close_at
    ):
        cancel_entry("FORCE_CLOSE")
        if state.position is None:
            state = state.model_copy(
                update={
                    "scheduled_force_close": None,
                    "force_close_state": "completed",
                    "status": "ACTIVE",
                }
            )
        elif state.close_intent is None:
            scheduled = state.scheduled_force_close
            if scheduled is None:
                intent = _close_intent(
                    config,
                    state,
                    event_input,
                    None,
                    config.force_close_at,
                    "FORCE_CLOSE",
                    expires_at=cast(datetime, config.end_at),
                )
                scheduled = OrderState(
                    intent=intent,
                    order_sequence=state.next_order_sequence,
                    status="PENDING",
                    status_reason="ORDER_SUBMITTED",
                    activated_at=None,
                    fill_sequence=None,
                    source_setup_id=None,
                    originating_decision_id=None,
                    creation_cause="FORCE_CLOSE",
                )
                intents.append(intent)
                state = state.model_copy(
                    update={
                        "scheduled_force_close": scheduled,
                        "next_order_sequence": state.next_order_sequence + 1,
                    }
                )
            activated = scheduled.model_copy(
                update={
                    "status": "ACTIVE",
                    "status_reason": "ORDER_ACTIVATED",
                    "activated_at": config.force_close_at,
                }
            )
            state = state.model_copy(
                update={
                    "scheduled_force_close": None,
                    "close_intent": activated,
                    "status": "CLOSING",
                    "force_close_state": "pending",
                }
            )
            state, emitted = _order_transition(
                config,
                state,
                event_input,
                correlation,
                activated,
                "PENDING",
                "ACTIVE",
                "ORDER_ACTIVATED",
                config.force_close_at,
            )
            events.append(emitted)

    if boundary >= config.contract.liquidation_start_at:
        cancel_entry("CONTRACT_LIQUIDATION")
        if state.position is None:
            state = state.model_copy(
                update={"status": "STOPPED", "finished_reason": "CONTRACT_CLOSED"}
            )
            return state, tuple(intents), tuple(events)
        if state.close_intent is not None and state.close_intent.creation_cause == "EXIT_RULE":
            old = state.close_intent
            expired = old.model_copy(update={"status": "EXPIRED", "status_reason": "ORDER_EXPIRED"})
            state = state.model_copy(update={"close_intent": None})
            state, emitted = _order_transition(
                config,
                state,
                event_input,
                correlation,
                expired,
                old.status,
                "EXPIRED",
                "ORDER_EXPIRED",
                config.contract.liquidation_start_at,
            )
            events.append(emitted)
        if state.close_intent is None:
            intent = _close_intent(
                config,
                state,
                event_input,
                None,
                config.contract.liquidation_start_at,
                "CONTRACT_LIQUIDATION",
            )
            direct = intent.active_from <= boundary
            order = OrderState(
                intent=intent,
                order_sequence=state.next_order_sequence,
                status="ACTIVE" if direct else "PENDING",
                status_reason="ORDER_ACTIVATED" if direct else "ORDER_SUBMITTED",
                activated_at=intent.active_from if direct else None,
                fill_sequence=None,
                source_setup_id=None,
                originating_decision_id=None,
                creation_cause="CONTRACT_LIQUIDATION",
            )
            intents.append(intent)
            state = state.model_copy(
                update={
                    "close_intent": order,
                    "status": "CLOSING",
                    "next_order_sequence": state.next_order_sequence + 1,
                }
            )
            state, emitted = _order_transition(
                config,
                state,
                event_input,
                correlation,
                order,
                "NOT_CREATED",
                order.status,
                order.status_reason,
                intent.active_from if direct else intent.submitted_at,
            )
            events.append(emitted)

    if boundary >= config.contract.last_trade_at and state.position is not None:
        close = state.close_intent
        if close is not None:
            expired = close.model_copy(
                update={"status": "EXPIRED", "status_reason": "ORDER_EXPIRED"}
            )
            state = state.model_copy(update={"close_intent": None})
            state, emitted = _order_transition(
                config,
                state,
                event_input,
                correlation,
                expired,
                close.status,
                "EXPIRED",
                "ORDER_EXPIRED",
                config.contract.last_trade_at,
            )
            events.append(emitted)
        state = state.model_copy(
            update={
                "status": "BLOCKED_EXPIRY_UNRESOLVED",
                "finished_reason": "BLOCKED_EXPIRY_UNRESOLVED",
            }
        )
    return state, tuple(intents), tuple(events)


def _advance_features(config: EngineConfig, state: EngineState, bar: FeatureBar) -> EngineState:
    runtime_by_id = {item.feature_id: item for item in state.feature_runtime}
    current: dict[str, Any] = {}
    trading_day = _trading_day(config.calendar.windows, bar.start_at)
    for feature in sorted(
        config.strategy_version.definition.features, key=lambda item: item.feature_id
    ):
        if feature.interval_seconds != int((bar.end_at - bar.start_at).total_seconds()):
            continue
        runtime, value = advance_feature(
            feature,
            runtime_by_id[feature.feature_id],
            bar,
            trading_day=trading_day,
            dependencies=current,
            tick_size=config.contract.tick_size,
        )
        runtime_by_id[feature.feature_id] = runtime
        current[feature.feature_id] = value
    latest = {value.feature_id: value for value in state.feature_values}
    latest.update(current)
    return state.model_copy(
        update={
            "feature_runtime": tuple(
                sorted(runtime_by_id.values(), key=lambda item: item.feature_id)
            ),
            "feature_values": tuple(
                sorted(
                    latest.values(),
                    key=lambda item: (
                        item.interval_seconds,
                        item.evaluation_bar_end,
                        item.feature_id,
                    ),
                )
            ),
        }
    )


def _consume_completed(
    config: EngineConfig,
    state: EngineState,
    event: CompletedBarEvent,
    correlation: str,
) -> EngineStepResult:
    selection = event.selection
    bar = selection.bar
    buckets = {(item.interval_seconds, item.start_at): item for item in state.interval_buckets}
    completed: list[IntervalBucket] = []
    for interval in _required_intervals(config):
        bounds = _bucket_bounds(config, bar.start_at, interval)
        if bounds is None:
            continue
        start, end = bounds
        key = (interval, start)
        existing = buckets.get(key)
        selections = (*existing.selections, selection) if existing else (selection,)
        revisions = (
            (*existing.published_base_revision_ids, event.published_base_revision_id)
            if existing
            else (event.published_base_revision_id,)
        )
        expected = interval // 60
        bucket = IntervalBucket(
            interval_seconds=interval,
            slot_index=(
                existing.slot_index
                if existing
                else (
                    (
                        state.last_source_slot_index
                        if state.last_source_slot_index is not None
                        else -1
                    )
                    + 1
                )
            ),
            start_at=start,
            end_at=end,
            selections=selections,
            published_base_revision_ids=revisions,
            expected_component_count=expected,
            status="complete" if len(selections) == expected and bar.end_at == end else "building",
        )
        if bucket.status == "complete":
            completed.append(bucket)
            buckets.pop(key, None)
        else:
            buckets[key] = bucket
    state = state.model_copy(
        update={
            "interval_buckets": tuple(
                sorted(buckets.values(), key=lambda item: (item.interval_seconds, item.start_at))
            ),
            "canonical_bar_count": state.canonical_bar_count + 1,
            "last_source_slot_index": (
                state.last_source_slot_index if state.last_source_slot_index is not None else -1
            )
            + 1,
        }
    )
    decisions: list[Decision] = []
    intents: list[OrderIntent] = []
    protective: list[ProtectiveOrderState] = []
    fills: list[Fill] = []
    fill_evidence: list[FillEvidence] = []
    mark_evidence: list[MarkEvidence] = []
    events: list[RunEvent] = []
    aggregate_by_interval = {item.interval_seconds: _aggregate(item) for item in completed}
    fill_bar = aggregate_by_interval.get(config.fill_model.fill_interval_seconds)
    if fill_bar is not None:
        state, new_intents, new_protective, new_fills, new_fill_ev, new_events = _process_fill_bar(
            config, state, event, correlation, fill_bar
        )
        intents.extend(new_intents)
        protective.extend(new_protective)
        fills.extend(new_fills)
        fill_evidence.extend(new_fill_ev)
        events.extend(new_events)
        state, mark_events, mark_ev = _mark(config, state, event, correlation, fill_bar)
        events.extend(mark_events)
        mark_evidence.append(mark_ev)
        state = state.model_copy(
            update={
                "last_fill_slot_index": (
                    state.last_fill_slot_index if state.last_fill_slot_index is not None else -1
                )
                + 1
            }
        )
    for interval, feature_bar in sorted(aggregate_by_interval.items()):
        state = _advance_features(config, state, feature_bar)
    execution_bar = aggregate_by_interval.get(config.strategy_version.execution_interval_seconds)
    if execution_bar is not None:
        state = state.model_copy(
            update={
                "last_execution_slot_index": (
                    state.last_execution_slot_index
                    if state.last_execution_slot_index is not None
                    else -1
                )
                + 1
            }
        )
        if execution_bar.end_at > config.start_at:
            state, decision, intent = _create_decision(
                config, state, event, correlation, execution_bar
            )
            decisions.append(decision)
            if intent is not None:
                intents.append(intent)
            state, emitted = _emit_event(
                config,
                state,
                event,
                correlation,
                event_type="DECISION_RECORDED",
                effective_at=decision.effective_at,
                payload={
                    "kind": "DECISION_RECORDED",
                    "decision_id": decision.decision_id,
                    "decision_sequence": decision.decision_sequence,
                },
                causation_id=decision.decision_id,
            )
            events.append(emitted)
            if intent is not None:
                order = state.pending_entry or state.close_intent
                assert order is not None
                destination = order.status
                state, emitted = _order_transition(
                    config,
                    state,
                    event,
                    correlation,
                    order,
                    "NOT_CREATED",
                    destination,
                    "ORDER_ACTIVATED" if destination == "ACTIVE" else "ORDER_SUBMITTED",
                    order.intent.active_from
                    if destination == "ACTIVE"
                    else order.intent.submitted_at,
                )
                events.append(emitted)
    return EngineStepResult(
        state=state,
        pre_state_sha256="0" * 64,
        post_state_sha256=_state_hash(state),
        decisions=tuple(decisions),
        intents=tuple(intents),
        protective_orders=tuple(protective),
        fills=tuple(fills),
        fill_evidence=tuple(fill_evidence),
        mark_evidence=tuple(mark_evidence),
        events=tuple(events),
        replayed=False,
    )


def step_engine(
    config: EngineConfig, state: EngineState, event: EngineInputEvent
) -> EngineStepResult:
    pre_state_sha256 = _state_hash(state)
    _validate_state_config(config, state)
    try:
        parsed = _EVENT_ADAPTER.validate_python(event)
    except ValidationError:
        _raise(EngineErrorCode.VALIDATION_ERROR)
    if isinstance(parsed, CompletedBarEvent) and parsed.selection.correction_observations:
        _validation("/selection/correction_observations", "UNSUPPORTED_CORRECTION_OBSERVATION")
    semantic_sha = _hash(_event_projection(parsed))
    start, end, _ = _identity(parsed)
    if (
        state.last_input_sha256 == semantic_sha
        and state.last_input_kind == parsed.kind
        and state.last_input_start_at == start
        and state.last_input_end_at == end
    ):
        return _empty(state, True)
    if (
        state.last_input_end_at is not None
        and state.last_input_start_at is not None
        and start < state.last_input_end_at
        and end > state.last_input_start_at
    ):
        _raise(
            EngineErrorCode.DUPLICATE_CONFLICT,
            {
                "kind": "duplicate_conflict",
                "logical_start_at": start,
                "logical_end_at": end,
                "previous_input_sha256": cast(str, state.last_input_sha256),
                "input_sha256": semantic_sha,
            },
        )
    if state.last_input_start_at == start and state.last_input_end_at == end:
        _raise(
            EngineErrorCode.DUPLICATE_CONFLICT,
            {
                "kind": "duplicate_conflict",
                "logical_start_at": start,
                "logical_end_at": end,
                "previous_input_sha256": cast(str, state.last_input_sha256),
                "input_sha256": semantic_sha,
            },
        )
    if state.status in _TERMINAL:
        _raise(
            EngineErrorCode.RUN_FINISHED,
            {
                "kind": "run_finished",
                "status": state.status,
                "finished_reason": state.finished_reason,
            },
        )
    if state.last_recorded_at is not None and parsed.recorded_at < state.last_recorded_at:
        _raise(
            EngineErrorCode.EVENT_OUT_OF_ORDER,
            {
                "kind": "event_out_of_order",
                "previous_start_at": cast(datetime, state.last_input_start_at),
                "previous_end_at": cast(datetime, state.last_input_end_at),
                "previous_recorded_at": state.last_recorded_at,
                "input_start_at": start,
                "input_end_at": end,
                "input_recorded_at": parsed.recorded_at,
            },
        )
    if (
        state.last_input_end_at is not None
        and state.last_input_start_at is not None
        and end <= state.last_input_start_at
    ):
        _raise(
            EngineErrorCode.EVENT_OUT_OF_ORDER,
            {
                "kind": "event_out_of_order",
                "previous_start_at": state.last_input_start_at,
                "previous_end_at": state.last_input_end_at,
                "previous_recorded_at": cast(datetime, state.last_recorded_at),
                "input_start_at": start,
                "input_end_at": end,
                "input_recorded_at": parsed.recorded_at,
            },
        )
    delta = 0
    if isinstance(parsed, CompletedBarEvent):
        _validate_completed(config, state, parsed)
        delta = 1
    elif isinstance(parsed, DataQualityEvent):
        delta = _validate_quality(config, state, parsed)
    if state.canonical_bar_count + delta > config.max_canonical_bars:
        _raise(
            EngineErrorCode.BATCH_LIMIT_EXCEEDED,
            {
                "kind": "batch_limit_exceeded",
                "current_count": state.canonical_bar_count,
                "input_delta": delta,
                "max_canonical_bars": config.max_canonical_bars,
            },
        )
    correlation = _input_correlation(config, state, parsed, semantic_sha)
    cursor_state = _event_cursor_state(state, parsed, semantic_sha)
    lifecycle_intents: tuple[OrderIntent, ...] = ()
    lifecycle_events: tuple[RunEvent, ...] = ()
    if not isinstance(parsed, FinishRunEvent):
        cursor_state, lifecycle_intents, lifecycle_events = _lifecycle_boundary(
            config, cursor_state, parsed, correlation, start
        )
        if cursor_state.status in _TERMINAL:
            cursor_state = cursor_state.model_copy(
                update={"canonical_bar_count": cursor_state.canonical_bar_count + delta}
            )
            return EngineStepResult(
                state=cursor_state,
                pre_state_sha256=pre_state_sha256,
                post_state_sha256=_state_hash(cursor_state),
                decisions=(),
                intents=lifecycle_intents,
                protective_orders=(),
                fills=(),
                fill_evidence=(),
                mark_evidence=(),
                events=lifecycle_events,
                replayed=False,
            )
    if isinstance(parsed, CompletedBarEvent):
        result = _consume_completed(config, cursor_state, parsed, correlation)
        return result.model_copy(
            update={
                "pre_state_sha256": pre_state_sha256,
                "intents": lifecycle_intents + result.intents,
                "events": lifecycle_events + result.events,
            }
        )
    if isinstance(parsed, DataQualityEvent):
        runtimes = cursor_state.feature_runtime
        if parsed.status in {"missing", "invalid"}:
            runtimes = tuple(break_runtime(item) for item in runtimes)
            cursor_state = cursor_state.model_copy(
                update={
                    "feature_runtime": runtimes,
                    "mark_status": "stale" if cursor_state.last_mark is not None else "none",
                    "interval_buckets": tuple(
                        item.model_copy(update={"status": "broken"})
                        for item in cursor_state.interval_buckets
                    ),
                    "canonical_bar_count": cursor_state.canonical_bar_count + delta,
                    "last_source_slot_index": (
                        cursor_state.last_source_slot_index
                        if cursor_state.last_source_slot_index is not None
                        else -1
                    )
                    + delta,
                }
            )
        cursor_state, emitted = _emit_event(
            config,
            cursor_state,
            parsed,
            correlation,
            event_type="DATA_QUALITY",
            effective_at=parsed.end_at,
            payload={
                "kind": "DATA_QUALITY",
                "start_at": parsed.start_at,
                "end_at": parsed.end_at,
                "status": parsed.status,
                "reason": parsed.reason,
                "source_bar_record_ids": parsed.source_bar_record_ids,
            },
            causation_id=None,
        )
        return EngineStepResult(
            state=cursor_state,
            pre_state_sha256=pre_state_sha256,
            post_state_sha256=_state_hash(cursor_state),
            decisions=(),
            intents=lifecycle_intents,
            protective_orders=(),
            fills=(),
            fill_evidence=(),
            mark_evidence=(),
            events=lifecycle_events + (emitted,),
            replayed=False,
        )
    assert isinstance(parsed, FinishRunEvent)
    failures = []
    if parsed.effective_at != config.end_at or config.mode != "backtest":
        failures.append("/effective_at")
    if parsed.recorded_at < parsed.effective_at:
        failures.append("/recorded_at")
    if parsed.emission_context.order_submitted_at < parsed.recorded_at:
        failures.append("/emission_context/order_submitted_at")
    if parsed.emission_context.output_recorded_at < parsed.emission_context.order_submitted_at:
        failures.append("/emission_context/output_recorded_at")
    if config.mode == "backtest" and not (
        parsed.recorded_at
        == parsed.emission_context.order_submitted_at
        == parsed.emission_context.output_recorded_at
        == parsed.effective_at
    ):
        if parsed.recorded_at != parsed.effective_at:
            failures.append("/recorded_at")
        if parsed.emission_context.order_submitted_at != parsed.effective_at:
            failures.append("/emission_context/order_submitted_at")
        if parsed.emission_context.output_recorded_at != parsed.effective_at:
            failures.append("/emission_context/output_recorded_at")
    if failures:
        _validation(min(failures), "FINISH_EVENT")
    if config.end_policy == "force_close" and cursor_state.position is not None:
        status, reason, force_state = "BLOCKED_UNCLOSED", "BLOCKED_UNCLOSED", "blocked"
    else:
        status, reason, force_state = (
            "FINISHED",
            "MARK_OPEN" if config.end_policy == "mark_open" else "FORCE_CLOSED",
            "completed" if config.end_policy == "force_close" else cursor_state.force_close_state,
        )
    cursor_state = cursor_state.model_copy(
        update={"status": status, "finished_reason": reason, "force_close_state": force_state}
    )
    cursor_state, emitted = _emit_event(
        config,
        cursor_state,
        parsed,
        correlation,
        event_type="RUN_FINISHED",
        effective_at=parsed.effective_at,
        payload={
            "kind": "RUN_FINISHED",
            "status": status,
            "end_policy": config.end_policy,
            "cash": cursor_state.cash,
            "equity": cursor_state.equity,
            "realized_pnl": cursor_state.realized_pnl,
            "unrealized_pnl": cursor_state.unrealized_pnl,
            "position_open": cursor_state.position is not None,
            "pending_entry": cursor_state.pending_entry is not None,
            "mark_status": cursor_state.mark_status,
            "reason": reason,
        },
        causation_id=None,
    )
    return EngineStepResult(
        state=cursor_state,
        pre_state_sha256=pre_state_sha256,
        post_state_sha256=_state_hash(cursor_state),
        decisions=(),
        intents=(),
        protective_orders=(),
        fills=(),
        fill_evidence=(),
        mark_evidence=(),
        events=(emitted,),
        replayed=False,
    )


def run_engine(
    config: EngineConfig,
    events: tuple[EngineInputEvent, ...],
    *,
    initial_state: EngineState | None = None,
) -> EngineRunResult:
    state = initialize_engine(config) if initial_state is None else initial_state
    if initial_state is not None:
        _validate_state_config(config, state)
    run_pre_sha256 = _state_hash(state)
    # Preflight the whole accepted batch so the state cannot partially advance past its cap.
    projected = state.canonical_bar_count
    previous_hash = state.last_input_sha256
    previous_identity = (state.last_input_kind, state.last_input_start_at, state.last_input_end_at)
    previous_recorded = state.last_recorded_at
    for raw in events:
        try:
            event = _EVENT_ADAPTER.validate_python(raw)
        except ValidationError:
            _raise(EngineErrorCode.VALIDATION_ERROR)
        if isinstance(event, CompletedBarEvent) and event.selection.correction_observations:
            _validation(
                "/selection/correction_observations",
                "UNSUPPORTED_CORRECTION_OBSERVATION",
            )
        sha = _hash(_event_projection(event))
        start, end, _ = _identity(event)
        identity = (event.kind, start, end)
        if sha == previous_hash and identity == previous_identity:
            continue
        previous_start, previous_end = previous_identity[1], previous_identity[2]
        if (
            previous_start is not None
            and previous_end is not None
            and (
                (start < previous_end and end > previous_start)
                or (start == previous_start and end == previous_end)
            )
        ):
            _raise(
                EngineErrorCode.DUPLICATE_CONFLICT,
                {
                    "kind": "duplicate_conflict",
                    "logical_start_at": start,
                    "logical_end_at": end,
                    "previous_input_sha256": cast(str, previous_hash),
                    "input_sha256": sha,
                },
            )
        if previous_recorded is not None and event.recorded_at < previous_recorded:
            _raise(
                EngineErrorCode.EVENT_OUT_OF_ORDER,
                {
                    "kind": "event_out_of_order",
                    "previous_start_at": cast(datetime, previous_start),
                    "previous_end_at": cast(datetime, previous_end),
                    "previous_recorded_at": previous_recorded,
                    "input_start_at": start,
                    "input_end_at": end,
                    "input_recorded_at": event.recorded_at,
                },
            )
        projected += (
            1
            if isinstance(event, CompletedBarEvent)
            else int((event.end_at - event.start_at).total_seconds() // 60)
            if isinstance(event, DataQualityEvent) and event.status != "closed"
            else 0
        )
        previous_hash, previous_identity = sha, identity
        previous_recorded = event.emission_context.output_recorded_at
    if projected > config.max_canonical_bars:
        _raise(
            EngineErrorCode.BATCH_LIMIT_EXCEEDED,
            {
                "kind": "batch_limit_exceeded",
                "current_count": state.canonical_bar_count,
                "input_delta": projected - state.canonical_bar_count,
                "max_canonical_bars": config.max_canonical_bars,
            },
        )
    decisions: list[Decision] = []
    intents: list[OrderIntent] = []
    protective: list[ProtectiveOrderState] = []
    fills: list[Fill] = []
    fill_ev: list[FillEvidence] = []
    mark_ev: list[MarkEvidence] = []
    emitted: list[RunEvent] = []
    step_hashes: list[EngineStepHash] = []
    for event in events:
        result = step_engine(config, state, event)
        state = result.state
        decisions.extend(result.decisions)
        intents.extend(result.intents)
        protective.extend(result.protective_orders)
        fills.extend(result.fills)
        fill_ev.extend(result.fill_evidence)
        mark_ev.extend(result.mark_evidence)
        emitted.extend(result.events)
        step_hashes.append(
            EngineStepHash(
                input_index=len(step_hashes),
                pre_state_sha256=result.pre_state_sha256,
                post_state_sha256=result.post_state_sha256,
                replayed=result.replayed,
            )
        )
    checkpoint = checkpoint_engine(config, state)
    return EngineRunResult(
        state=state,
        pre_state_sha256=run_pre_sha256,
        post_state_sha256=_state_hash(state),
        step_hashes=tuple(step_hashes),
        decisions=tuple(decisions),
        intents=tuple(intents),
        protective_orders=tuple(protective),
        fills=tuple(fills),
        fill_evidence=tuple(fill_ev),
        mark_evidence=tuple(mark_ev),
        events=tuple(emitted),
        checkpoint=checkpoint,
    )


def checkpoint_engine(config: EngineConfig, state: EngineState) -> EngineCheckpoint:
    _validate_state_config(config, state)
    return EngineCheckpoint(
        schema_version="v1",
        format_version=1,
        engine_version="paper-engine-v1",
        config_sha256=state.config_sha256,
        state=state,
        state_sha256=_state_hash(state),
    )


def _validate_checkpoint_state(state: EngineState) -> None:
    failures: list[str] = []
    terminal_pairs: dict[str, set[str | None]] = {
        "ACTIVE": {None},
        "CLOSING": {None},
        "FINISHED": {"MARK_OPEN", "FORCE_CLOSED"},
        "STOPPED": {"CONTRACT_CLOSED"},
        "BLOCKED_UNCLOSED": {"BLOCKED_UNCLOSED"},
        "BLOCKED_EXPIRY_UNRESOLVED": {"BLOCKED_EXPIRY_UNRESOLVED"},
    }
    if state.finished_reason not in terminal_pairs[state.status]:
        failures.append("/state/finished_reason")
    entry = state.pending_entry
    if entry is not None and (
        entry.originating_decision_id is None or entry.creation_cause != "ENTRY"
    ):
        failures.append("/state/pending_entry/originating_decision_id")
    if state.position is not None:
        stop = state.position.protective_bracket.stop
        target = state.position.protective_bracket.target
        if stop.originating_decision_id != target.originating_decision_id:
            failures.append("/state/position/protective_bracket/target/originating_decision_id")
    scheduled = state.scheduled_force_close
    if scheduled is not None and (
        scheduled.originating_decision_id is not None or scheduled.creation_cause != "FORCE_CLOSE"
    ):
        failures.append("/state/scheduled_force_close/originating_decision_id")
    close = state.close_intent
    if close is not None:
        valid = (
            close.originating_decision_id is not None and close.creation_cause == "EXIT_RULE"
        ) or (
            close.originating_decision_id is None
            and close.creation_cause in {"CONTRACT_LIQUIDATION", "FORCE_CLOSE"}
        )
        if not valid:
            failures.append("/state/close_intent/originating_decision_id")
    if failures:
        _raise(
            EngineErrorCode.CHECKPOINT_MISMATCH,
            {
                "kind": "checkpoint_mismatch",
                "path": min(failures),
                "reason": "STATE_INVARIANT",
            },
        )


def restore_engine(config: EngineConfig, checkpoint: EngineCheckpoint) -> EngineState:
    try:
        parsed = EngineCheckpoint.model_validate(checkpoint, strict=True)
    except ValidationError:
        _raise(EngineErrorCode.CHECKPOINT_MISMATCH)
    if parsed.schema_version != "v1":
        _raise(EngineErrorCode.CHECKPOINT_MISMATCH, "SCHEMA_VERSION")
    if parsed.format_version != 1:
        _raise(EngineErrorCode.CHECKPOINT_MISMATCH, "FORMAT_VERSION")
    if parsed.engine_version != "paper-engine-v1":
        _raise(EngineErrorCode.CHECKPOINT_MISMATCH, "ENGINE_VERSION")
    if parsed.state_sha256 != _state_hash(parsed.state):
        _raise(EngineErrorCode.CHECKPOINT_MISMATCH, "STATE_HASH")
    if parsed.config_sha256 != parsed.state.config_sha256:
        _raise(
            EngineErrorCode.CHECKPOINT_MISMATCH,
            {
                "kind": "checkpoint_mismatch",
                "path": "/config_sha256",
                "reason": "CHECKPOINT_CONFIG_HASH",
            },
        )
    if _config_hash(parsed.state.config_snapshot) != parsed.state.config_sha256:
        _raise(
            EngineErrorCode.CHECKPOINT_MISMATCH,
            {
                "kind": "checkpoint_mismatch",
                "path": "/state/config_snapshot",
                "reason": "CHECKPOINT_CONFIG_HASH",
            },
        )
    _validate_checkpoint_state(parsed.state)
    _validate_state_config(config, parsed.state)
    return parsed.state
