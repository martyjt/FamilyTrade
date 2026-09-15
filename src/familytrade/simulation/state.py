"""Strict deterministic runtime records for the FT-07 paper engine."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_serializer,
    model_validator,
)

from familytrade.market_data.models import (
    BarSelection,
    CalendarVersion,
    DatasetRevision,
    FuturesContract,
)
from familytrade.strategies.definitions import EntryWindow, StopSpec, StrategyVersion, TargetSpec

Uuid7 = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"),
]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PositiveStrictInt = Annotated[int, Field(strict=True, gt=0)]
NonNegativeStrictInt = Annotated[int, Field(strict=True, ge=0)]
Money = Decimal


class FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True, validate_default=True
    )

    @model_validator(mode="after")
    def _strict_common_values(self) -> FrozenModel:
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, datetime) and (
                value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)
            ):
                raise ValueError(f"{name} must be an aware UTC datetime")
            if isinstance(value, Decimal) and not value.is_finite():
                raise ValueError(f"{name} must be finite")
        return self

    @field_serializer("*", when_used="json", check_fields=False)
    def _serialize_decimal(self, value: object) -> object:
        if isinstance(value, Decimal):
            text = format(value, "f")
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return "0" if text in {"", "-0"} else text
        return value


class CostModel(FrozenModel):
    commission_per_contract_per_side: Annotated[Decimal, Field(ge=0, le=1000)]
    currency: Literal["USD"]
    market_slippage_ticks: Annotated[int, Field(strict=True, ge=0, le=100)]
    stop_slippage_ticks: Annotated[int, Field(strict=True, ge=0, le=100)]
    limit_slippage_ticks: Annotated[int, Field(strict=True, ge=0, le=100)]


class FillModel(FrozenModel):
    fill_interval_seconds: Annotated[int, Field(strict=True, gt=0)]
    partial_fill_policy: Literal["all_or_none"]
    both_hit_policy: Literal["stop_first", "target_first"]
    entry_bar_exit_policy: Literal["conservative_stop_first", "next_bar_only"]


class FixedContractsSizing(FrozenModel):
    kind: Literal["fixed_contracts"]
    quantity: Annotated[int, Field(strict=True, ge=1, le=100)]


class StopRiskFractionSizing(FrozenModel):
    kind: Literal["stop_risk_fraction"]
    fraction: Annotated[Decimal, Field(gt=0, le=Decimal("0.05"))]
    max_quantity: Annotated[int, Field(strict=True, ge=1, le=100)]


SizingPolicy = Annotated[FixedContractsSizing | StopRiskFractionSizing, Field(discriminator="kind")]


class RiskPolicy(FrozenModel):
    per_entry_loss_cap: Annotated[Decimal, Field(gt=0, le=1_000_000_000)]
    daily_loss_cap: Annotated[Decimal, Field(gt=0, le=1_000_000_000)]
    cumulative_drawdown_cap: Annotated[Decimal, Field(gt=0, le=1_000_000_000)]
    max_positions: Literal[1]
    max_entries_per_trading_day: Annotated[int, Field(strict=True, ge=1, le=1000)]


class EngineConfig(FrozenModel):
    schema_version: Literal["v1"]
    engine_version: Literal["paper-engine-v1"]
    run_id: Uuid7
    lane_id: Uuid7 | None
    mode: Literal["backtest", "forward_paper"]
    owner_user_id: Uuid7
    strategy_version: StrategyVersion
    contract: FuturesContract
    calendar: CalendarVersion
    dataset_revision: DatasetRevision | None
    source: Annotated[str, Field(min_length=1, max_length=80)]
    price_basis: Literal["trades"]
    entry_windows: tuple[EntryWindow, ...]
    start_at: datetime
    end_at: datetime | None
    force_close_at: datetime | None
    starting_cash: Annotated[Decimal, Field(gt=0)]
    base_currency: Literal["USD"]
    cost_model: CostModel
    fill_model: FillModel
    sizing_policy: SizingPolicy
    risk_policy: RiskPolicy
    end_policy: Literal["mark_open", "force_close"]
    max_canonical_bars: Annotated[int, Field(strict=True)] = 500_000
    random_seed: None


class EmissionContext(FrozenModel):
    attempt_id: Uuid7 | None
    fencing_token: PositiveStrictInt
    order_submitted_at: datetime
    output_recorded_at: datetime


class CompletedBarEvent(FrozenModel):
    kind: Literal["completed_bar_v1"]
    selection: BarSelection
    published_base_revision_id: Uuid7 | None
    recorded_at: datetime
    emission_context: EmissionContext


class DataQualityEvent(FrozenModel):
    kind: Literal["data_quality_v1"]
    start_at: datetime
    end_at: datetime
    status: Literal["missing", "invalid", "closed"]
    reason: Literal["NO_BAR", "INVALID_BAR", "MAINTENANCE", "SCHEDULED_CLOSED"]
    source_bar_record_ids: tuple[str, ...]
    recorded_at: datetime
    emission_context: EmissionContext


class FinishRunEvent(FrozenModel):
    kind: Literal["finish_run_v1"]
    effective_at: datetime
    recorded_at: datetime
    emission_context: EmissionContext


EngineInputEvent = Annotated[
    CompletedBarEvent | DataQualityEvent | FinishRunEvent, Field(discriminator="kind")
]


class SourceDatasetProvenance(FrozenModel):
    bar_record_id: str
    published_base_revision_id: Uuid7 | None


FeatureScalar = Decimal | int | bool | str | None


class FeatureValue(FrozenModel):
    feature_id: str
    interval_seconds: PositiveStrictInt
    evaluation_bar_end: datetime
    value_type: str
    unit: str
    value: FeatureScalar
    status: Literal["KNOWN", "UNKNOWN"]
    reason_code: str | None
    source_bar_record_ids: tuple[str, ...]
    source_dataset_provenance: tuple[SourceDatasetProvenance, ...]
    known_at: datetime


class AccumulatorPoint(FrozenModel):
    evaluation_bar_end: datetime
    known_at: datetime
    value: Decimal
    source_bar_record_ids: tuple[str, ...]
    source_dataset_provenance: tuple[SourceDatasetProvenance, ...]


class NoneAccumulator(FrozenModel):
    kind: Literal["none_v1"]


class RollingWindowAccumulator(FrozenModel):
    kind: Literal["rolling_window_v1"]
    points: tuple[AccumulatorPoint, ...]
    sum: Decimal


class EmaAccumulator(FrozenModel):
    kind: Literal["ema_v1"]
    seed_points: tuple[AccumulatorPoint, ...]
    seed_sum: Decimal
    ema: Decimal | None


class RsiWilderAccumulator(FrozenModel):
    kind: Literal["rsi_wilder_v1"]
    previous_close: AccumulatorPoint | None
    delta_count: NonNegativeStrictInt
    seed_gain_sum: Decimal
    seed_loss_sum: Decimal
    average_gain: Decimal | None
    average_loss: Decimal | None


class AtrWilderAccumulator(FrozenModel):
    kind: Literal["atr_wilder_v1"]
    previous_close: AccumulatorPoint | None
    true_range_count: NonNegativeStrictInt
    seed_true_range_sum: Decimal
    average_true_range: Decimal | None


class RelativeVolumeAccumulator(FrozenModel):
    kind: Literal["relative_volume_v1"]
    prior_volumes: tuple[AccumulatorPoint, ...]
    prior_volume_sum: Decimal


class SessionVwapAccumulator(FrozenModel):
    kind: Literal["session_vwap_v1"]
    trading_day: date | None
    typical_volume_numerator: Decimal
    volume_denominator: Decimal
    source_bar_record_ids: tuple[str, ...]
    source_dataset_provenance: tuple[SourceDatasetProvenance, ...]


class PivotAccumulator(FrozenModel):
    kind: Literal["pivot_v1"]
    candidate_bars: tuple[BarSelection, ...]
    candidate_source_dataset_provenance: tuple[SourceDatasetProvenance, ...]
    confirmed_highs: tuple[AccumulatorPoint, ...]
    confirmed_lows: tuple[AccumulatorPoint, ...]


class SessionLevelAccumulator(FrozenModel):
    kind: Literal["session_level_v1"]
    current_trading_day: date | None
    current_high: Decimal | None
    current_low: Decimal | None
    current_complete: bool
    previous_trading_day: date | None
    previous_high: Decimal | None
    previous_low: Decimal | None
    previous_complete: bool
    current_source_bar_record_ids: tuple[str, ...]
    current_source_dataset_provenance: tuple[SourceDatasetProvenance, ...]
    previous_source_bar_record_ids: tuple[str, ...]
    previous_source_dataset_provenance: tuple[SourceDatasetProvenance, ...]


class DependencyAccumulator(FrozenModel):
    kind: Literal["dependency_v1"]
    dependency_feature_ids: tuple[str, ...]
    previous_values: tuple[FeatureValue, ...]


FeatureAccumulator = Annotated[
    NoneAccumulator
    | RollingWindowAccumulator
    | EmaAccumulator
    | RsiWilderAccumulator
    | AtrWilderAccumulator
    | RelativeVolumeAccumulator
    | SessionVwapAccumulator
    | PivotAccumulator
    | SessionLevelAccumulator
    | DependencyAccumulator,
    Field(discriminator="kind"),
]


class FeatureRuntimeState(FrozenModel):
    feature_id: str
    feature_name: str
    consecutive_bars: NonNegativeStrictInt
    accumulator: FeatureAccumulator
    history: tuple[FeatureValue, ...]
    session_trading_day: date | None
    continuity_status: Literal["clean", "broken_until_reseed", "broken_until_session"]


class RuleResult(FrozenModel):
    node_id: str
    result: Literal["PASS", "FAIL", "UNKNOWN"]
    value: Decimal | bool | str | None
    unit: str
    source_bar_record_ids: tuple[str, ...]
    known_at: datetime
    reason_code: str


class IntervalBucket(FrozenModel):
    interval_seconds: PositiveStrictInt
    slot_index: NonNegativeStrictInt
    start_at: datetime
    end_at: datetime
    selections: tuple[BarSelection, ...]
    published_base_revision_ids: tuple[Uuid7 | None, ...]
    expected_component_count: PositiveStrictInt
    status: Literal["building", "complete", "broken"]


class IntervalIndex(FrozenModel):
    interval_seconds: PositiveStrictInt
    last_slot_index: int | None


class ZoneState(FrozenModel):
    zone_id: Uuid7
    kind: Literal["support", "resistance"]
    low: Decimal
    high: Decimal
    creation_sequence: NonNegativeStrictInt
    touch_count: NonNegativeStrictInt
    created_zone_index: NonNegativeStrictInt
    last_touch_zone_index: NonNegativeStrictInt
    last_filled_execution_index: int | None
    pivot_bar_end: datetime
    confirmation_bar_end: datetime
    known_at: datetime
    source_bar_record_ids: tuple[str, ...]
    source_dataset_provenance: tuple[SourceDatasetProvenance, ...]


class SetupSnapshot(FrozenModel):
    setup_id: Uuid7
    family: Literal["reversal", "breakout"]
    side: Literal["long", "short"]
    setup_sequence: NonNegativeStrictInt
    source_zone_ids: tuple[Uuid7, ...]
    arm_execution_index: int | None
    signal_execution_index: int | None
    unit: str
    entry: Decimal
    stop: Decimal
    target: Decimal
    target_mode: str
    frozen_feature_values: tuple[FeatureValue, ...]
    source_bar_record_ids: tuple[str, ...]
    source_dataset_provenance: tuple[SourceDatasetProvenance, ...]
    known_at: datetime


class SetupState(FrozenModel):
    module_kind: str
    status: Literal["IDLE", "ARMED", "ENTRY_PENDING", "POSITION_OPEN", "EXPIRED", "CANCELLED"]
    snapshot: SetupSnapshot | None
    expires_after_execution_index: int | None
    reason_code: str | None


class BracketTemplate(FrozenModel):
    stop: StopSpec
    target: TargetSpec
    frozen_feature_values: dict[str, FeatureValue]


class OrderIntent(FrozenModel):
    order_id: Uuid7
    kind: Literal["entry", "close"]
    order_type: Literal["market", "limit"]
    side: Literal["buy", "sell"]
    effect: Literal["open", "close"]
    quantity: PositiveStrictInt
    raw_price: Decimal | None
    executable_price: Decimal | None
    raw_stop: Decimal | None
    stop_price: Decimal | None
    raw_target: Decimal | None
    target_price: Decimal | None
    bracket_template: BracketTemplate | None
    effective_at: datetime
    submitted_at: datetime
    active_from: datetime
    expires_at: datetime
    entry_bar_exit_policy: Literal["conservative_stop_first", "next_bar_only"]
    both_hit_policy: Literal["stop_first", "target_first"]


class OrderState(FrozenModel):
    intent: OrderIntent
    order_sequence: NonNegativeStrictInt
    status: Literal["PENDING", "ACTIVE", "FILLED", "EXPIRED", "CANCELLED"]
    status_reason: str
    activated_at: datetime | None
    fill_sequence: int | None
    source_setup_id: str | None
    originating_decision_id: Uuid7 | None
    creation_cause: Literal["ENTRY", "EXIT_RULE", "CONTRACT_LIQUIDATION", "FORCE_CLOSE"]


class ProtectiveOrderState(FrozenModel):
    order_id: Uuid7
    role: Literal["protective_stop", "profit_target"]
    order_type: Literal["stop_market", "limit"]
    side: Literal["buy", "sell"]
    effect: Literal["close"]
    quantity: PositiveStrictInt
    trigger_price: Decimal
    status: Literal["ACTIVE", "FILLED", "CANCELLED"]
    active_from: datetime
    filled_at: datetime | None
    cancelled_at: datetime | None
    status_reason: str
    entry_order_id: Uuid7
    entry_fill_id: Uuid7
    order_sequence: NonNegativeStrictInt
    fill_sequence: int | None
    originating_decision_id: Uuid7


class ProtectiveBracketState(FrozenModel):
    stop: ProtectiveOrderState
    target: ProtectiveOrderState
    both_hit_policy: Literal["stop_first", "target_first"]
    entry_bar_exit_policy: Literal["conservative_stop_first", "next_bar_only"]


class PositionState(FrozenModel):
    side: Literal["long", "short"]
    quantity: PositiveStrictInt
    contract_id: Uuid7
    entry_order_id: Uuid7
    entry_fill_id: Uuid7
    entry_price: Decimal
    opened_at: datetime
    opened_trading_day: date
    entry_commission: Decimal
    protective_bracket: ProtectiveBracketState


class RiskState(FrozenModel):
    trading_day: date | None
    daily_start_equity: Decimal
    filled_entries_in_trading_day: NonNegativeStrictInt
    latches: tuple[Literal["DAILY_LOSS_LIMIT", "CUMULATIVE_DRAWDOWN_LIMIT"], ...]
    high_water: Decimal
    current_drawdown: Decimal
    maximum_drawdown: Decimal


class DecisionEvidence(FrozenModel):
    evaluation_stage: Literal["entry_rule", "exit_rule", "arm", "entry_intent"]
    evidence_mode: Literal["historical", "contemporaneous", "reconstructed_after_outage"]
    results: tuple[RuleResult, ...]
    selected_setup: SetupSnapshot | None


class Decision(FrozenModel):
    schema_version: Literal["v1"]
    decision_id: Uuid7
    owner_user_id: Uuid7
    run_id: Uuid7
    lane_id: Uuid7 | None
    strategy_version_id: Uuid7
    dataset_revision_id: Uuid7 | None
    source_bar_record_ids: tuple[str, ...]
    execution_bar_end: datetime
    decision_sequence: NonNegativeStrictInt
    decision_type: Literal["HOLD", "ARM", "ENTRY", "CLOSE", "CANCEL", "REJECT"]
    side: Literal["long", "short"] | None
    setup_id: Uuid7 | None
    reason_code: str
    evidence: DecisionEvidence
    order_intent: OrderIntent | None
    pre_state_sha256: Sha256
    post_state_sha256: Sha256
    causation_event_id: Uuid7
    effective_at: datetime
    decided_at: datetime
    idempotency_key: Uuid7
    created_at: datetime
    record_version: Literal[1]


class Fill(FrozenModel):
    schema_version: Literal["v1"]
    fill_id: Uuid7
    owner_user_id: Uuid7
    run_id: Uuid7
    lane_id: Uuid7 | None
    order_id: Uuid7
    contract_id: Uuid7
    bar_record_id: str
    fill_sequence: NonNegativeStrictInt
    side: Literal["buy", "sell"]
    effect: Literal["open", "close"]
    quantity: PositiveStrictInt
    base_price: Decimal
    fill_price: Decimal
    slippage: Decimal
    commission: Decimal
    currency: Literal["USD"]
    model_time: datetime
    realized_pnl: Decimal
    cash_after: Decimal
    position_quantity_after: Annotated[int, Field(strict=True)]
    position_average_after: Decimal | None
    reason: str
    causation_decision_id: Uuid7 | None
    created_at: datetime
    fencing_token: PositiveStrictInt
    record_version: Literal[1]


class FillEvidence(FrozenModel):
    fill_id: Uuid7
    fill_bar_start_at: datetime
    fill_bar_end_at: datetime
    bar_record_id_role: Literal["availability_anchor"]
    availability_anchor_bar_record_id: str
    source_bar_record_ids: tuple[str, ...]
    source_dataset_provenance: tuple[SourceDatasetProvenance, ...]


class MarkEvidence(FrozenModel):
    run_event_id: Uuid7
    fill_bar_start_at: datetime
    fill_bar_end_at: datetime
    close_bar_record_id: str
    source_bar_record_ids: tuple[str, ...]
    source_dataset_provenance: tuple[SourceDatasetProvenance, ...]


class RunEvent(FrozenModel):
    schema_version: Literal["v1"]
    run_event_id: Uuid7
    owner_user_id: Uuid7
    aggregate_type: Literal["run", "lane"]
    aggregate_id: Uuid7
    sequence: NonNegativeStrictInt
    event_type: str
    effective_at: datetime
    recorded_at: datetime
    payload: dict[str, Any]
    causation_id: Uuid7 | None
    correlation_id: Uuid7
    state_sha256: Sha256
    attempt_id: Uuid7 | None
    fencing_token: PositiveStrictInt | None
    created_at: datetime
    record_version: Literal[1]


class EngineState(FrozenModel):
    schema_version: Literal["v1"]
    engine_version: Literal["paper-engine-v1"]
    config_snapshot: EngineConfig
    config_sha256: Sha256
    status: Literal[
        "ACTIVE", "CLOSING", "FINISHED", "STOPPED", "BLOCKED_UNCLOSED", "BLOCKED_EXPIRY_UNRESOLVED"
    ]
    last_input_kind: Literal["completed_bar_v1", "data_quality_v1", "finish_run_v1"] | None
    last_input_start_at: datetime | None
    last_input_end_at: datetime | None
    last_bar_start_at: datetime | None
    last_bar_record_id: str | None
    last_bar_payload_hash: str | None
    last_availability_at: datetime | None
    last_recorded_at: datetime | None
    last_input_sha256: Sha256 | None
    last_published_base_revision_id: Uuid7 | None
    canonical_bar_count: NonNegativeStrictInt
    last_source_slot_index: int | None
    last_fill_slot_index: int | None
    last_execution_slot_index: int | None
    zone_slot_indexes: tuple[IntervalIndex, ...]
    interval_buckets: tuple[IntervalBucket, ...]
    feature_values: tuple[FeatureValue, ...]
    feature_runtime: tuple[FeatureRuntimeState, ...]
    zones: tuple[ZoneState, ...]
    setups: tuple[SetupState, ...]
    pending_entry: OrderState | None
    position: PositionState | None
    close_intent: OrderState | None
    scheduled_force_close: OrderState | None
    cash: Decimal
    equity: Decimal
    fees: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    exposure_seconds: NonNegativeStrictInt
    last_mark: Decimal | None
    last_mark_bar_record_id: str | None
    last_mark_source_bar_record_ids: tuple[str, ...]
    last_mark_source_dataset_provenance: tuple[SourceDatasetProvenance, ...]
    mark_status: Literal["none", "fresh", "stale"]
    risk: RiskState
    next_decision_sequence: NonNegativeStrictInt
    next_order_sequence: NonNegativeStrictInt
    next_fill_sequence: NonNegativeStrictInt
    next_event_sequence: NonNegativeStrictInt
    next_zone_sequence: NonNegativeStrictInt
    next_setup_sequence: NonNegativeStrictInt
    next_correlation_sequence: NonNegativeStrictInt
    force_close_state: Literal["not_applicable", "waiting", "pending", "completed", "blocked"]
    finished_reason: (
        Literal[
            "MARK_OPEN",
            "FORCE_CLOSED",
            "CONTRACT_CLOSED",
            "BLOCKED_UNCLOSED",
            "BLOCKED_EXPIRY_UNRESOLVED",
        ]
        | None
    )

    @model_validator(mode="after")
    def validate_config_snapshot_size(self) -> EngineState:
        from familytrade.market_data.models import canonical_json_bytes

        if len(canonical_json_bytes(self.config_snapshot)) > 16_777_216:
            raise ValueError("config_snapshot exceeds 16777216 canonical bytes")
        return self


class EngineCheckpoint(FrozenModel):
    schema_version: Annotated[str, Field(strict=True)]
    format_version: Annotated[int, Field(strict=True)]
    engine_version: Annotated[str, Field(strict=True)]
    config_sha256: Sha256
    state: EngineState
    state_sha256: Sha256


class EngineStepHash(FrozenModel):
    input_index: NonNegativeStrictInt
    pre_state_sha256: Sha256
    post_state_sha256: Sha256
    replayed: bool


class EngineStepResult(FrozenModel):
    state: EngineState
    pre_state_sha256: Sha256
    post_state_sha256: Sha256
    decisions: tuple[Decision, ...]
    intents: tuple[OrderIntent, ...]
    protective_orders: tuple[ProtectiveOrderState, ...]
    fills: tuple[Fill, ...]
    fill_evidence: tuple[FillEvidence, ...]
    mark_evidence: tuple[MarkEvidence, ...]
    events: tuple[RunEvent, ...]
    replayed: bool

    @model_validator(mode="after")
    def validate_hashes_and_replay(self) -> EngineStepResult:
        from familytrade.market_data.models import canonical_json_bytes

        actual = hashlib.sha256(canonical_json_bytes(self.state)).hexdigest()
        if self.post_state_sha256 != actual:
            raise ValueError("post_state_sha256 must hash state")
        if self.replayed and (
            self.pre_state_sha256 != self.post_state_sha256
            or self.decisions
            or self.intents
            or self.protective_orders
            or self.fills
            or self.fill_evidence
            or self.mark_evidence
            or self.events
        ):
            raise ValueError("replayed result must be an empty unchanged delta")
        return self


class EngineRunResult(FrozenModel):
    state: EngineState
    pre_state_sha256: Sha256
    post_state_sha256: Sha256
    step_hashes: tuple[EngineStepHash, ...]
    decisions: tuple[Decision, ...]
    intents: tuple[OrderIntent, ...]
    protective_orders: tuple[ProtectiveOrderState, ...]
    fills: tuple[Fill, ...]
    fill_evidence: tuple[FillEvidence, ...]
    mark_evidence: tuple[MarkEvidence, ...]
    events: tuple[RunEvent, ...]
    checkpoint: EngineCheckpoint

    @model_validator(mode="after")
    def validate_hash_chain(self) -> EngineRunResult:
        from familytrade.market_data.models import canonical_json_bytes

        actual = hashlib.sha256(canonical_json_bytes(self.state)).hexdigest()
        if self.post_state_sha256 != actual or self.checkpoint.state_sha256 != actual:
            raise ValueError("run/checkpoint post hashes must hash state")
        if self.checkpoint.state != self.state:
            raise ValueError("run checkpoint state must equal returned state")
        if self.step_hashes:
            if self.step_hashes[0].pre_state_sha256 != self.pre_state_sha256:
                raise ValueError("first step pre hash must equal run pre hash")
            if self.step_hashes[-1].post_state_sha256 != self.post_state_sha256:
                raise ValueError("last step post hash must equal run post hash")
            for left, right in zip(self.step_hashes, self.step_hashes[1:], strict=False):
                if left.post_state_sha256 != right.pre_state_sha256:
                    raise ValueError("step hashes must form a contiguous chain")
        elif self.pre_state_sha256 != self.post_state_sha256:
            raise ValueError("empty run must not change state")
        return self


EngineErrorCode = Literal[
    "VALIDATION_ERROR",
    "INVALID_BAR_EVENT",
    "EVENT_AFTER_END",
    "BATCH_LIMIT_EXCEEDED",
    "UNSUPPORTED_CONFIGURATION",
    "UNSUPPORTED_FILL_INTERVAL",
    "UNSUPPORTED_CONTRACT_CHANGE",
    "CONFIG_MISMATCH",
    "CHECKPOINT_MISMATCH",
    "EVENT_OUT_OF_ORDER",
    "DUPLICATE_CONFLICT",
    "RUN_FINISHED",
]
JsonPointer = Annotated[StrictStr, Field(pattern=r"^(?:/(?:[^~/]|~[01])*)*$")]


class ValidationErrorDetails(FrozenModel):
    kind: Literal["validation_error"]
    path: JsonPointer
    reason: Literal[
        "BACKTEST_TIMING_MISMATCH",
        "DATA_QUALITY_COMBINATION",
        "FINISH_EVENT",
        "UNACCOUNTED_OPEN_INTERVAL",
        "UNSUPPORTED_CORRECTION_OBSERVATION",
        "INPUT_CROSS_FIELD",
    ]


class InvalidBarEventDetails(FrozenModel):
    kind: Literal["invalid_bar_event"]
    bar_record_id: StrictStr
    reason: Literal[
        "SELECTION_STATUS",
        "IDENTITY",
        "OHLCV",
        "PRICE_TICK",
        "INTERVAL",
        "AVAILABILITY",
        "CALENDAR",
    ]


class EventAfterEndDetails(FrozenModel):
    kind: Literal["event_after_end"]
    event_end_at: datetime
    config_end_at: datetime

    @model_validator(mode="after")
    def validate_order(self) -> EventAfterEndDetails:
        if self.event_end_at <= self.config_end_at:
            raise ValueError("event_end_at must be after config_end_at")
        return self


class BatchLimitExceededDetails(FrozenModel):
    kind: Literal["batch_limit_exceeded"]
    current_count: Annotated[StrictInt, Field(ge=0)]
    input_delta: Annotated[StrictInt, Field(gt=0)]
    max_canonical_bars: Annotated[StrictInt, Field(gt=0)]

    @model_validator(mode="after")
    def validate_exceeded(self) -> BatchLimitExceededDetails:
        if self.current_count + self.input_delta <= self.max_canonical_bars:
            raise ValueError("batch count must exceed max_canonical_bars")
        return self


class UnsupportedConfigurationDetails(FrozenModel):
    kind: Literal["unsupported_configuration"]
    path: JsonPointer
    reason: Literal[
        "FILL_INTERVAL_STRATEGY_MISMATCH",
        "OWNER_MISMATCH",
        "STRATEGY_NOT_EXECUTABLE",
        "STRATEGY_HASH_MISMATCH",
        "CATALOGUE_MISMATCH",
        "CONTRACT_CALENDAR_MISMATCH",
        "CONTRACT_NUMERIC_POLICY",
        "CURRENCY_MISMATCH",
        "BACKTEST_BINDING",
        "FORWARD_BINDING",
        "DATASET_BINDING",
        "DATASET_COVERAGE",
        "RUN_SPAN_EXCEEDED",
        "BAR_LIMIT_RANGE",
        "CONFIG_SNAPSHOT_LIMIT",
        "MODE_LANE_MISMATCH",
        "END_POLICY_MISMATCH",
        "POLICY_UNSUPPORTED",
    ]


class UnsupportedFillIntervalDetails(FrozenModel):
    kind: Literal["unsupported_fill_interval"]
    fill_interval_seconds: PositiveStrictInt
    execution_interval_seconds: PositiveStrictInt
    reason: Literal["SUBMINUTE", "NON_MINUTE", "LARGER_THAN_EXECUTION", "NOT_DIVISOR"]


class UnsupportedContractChangeDetails(FrozenModel):
    kind: Literal["unsupported_contract_change"]
    expected_contract_id: Uuid7
    actual_contract_id: Uuid7
    expected_record_version: PositiveStrictInt
    actual_record_version: PositiveStrictInt

    @model_validator(mode="after")
    def validate_changed(self) -> UnsupportedContractChangeDetails:
        if (
            self.expected_contract_id == self.actual_contract_id
            and self.expected_record_version == self.actual_record_version
        ):
            raise ValueError("contract id or version must differ")
        return self


class ConfigMismatchDetails(FrozenModel):
    kind: Literal["config_mismatch"]
    path: JsonPointer
    expected_config_sha256: Sha256
    actual_config_sha256: Sha256

    @model_validator(mode="after")
    def validate_changed(self) -> ConfigMismatchDetails:
        if self.expected_config_sha256 == self.actual_config_sha256:
            raise ValueError("config hashes must differ")
        return self


class CheckpointMismatchDetails(FrozenModel):
    kind: Literal["checkpoint_mismatch"]
    path: JsonPointer
    reason: Literal[
        "SCHEMA_VERSION",
        "FORMAT_VERSION",
        "ENGINE_VERSION",
        "CHECKPOINT_CONFIG_HASH",
        "STATE_HASH",
        "STATE_INVARIANT",
    ]

    @model_validator(mode="after")
    def validate_path(self) -> CheckpointMismatchDetails:
        expected = {
            "SCHEMA_VERSION": "/schema_version",
            "FORMAT_VERSION": "/format_version",
            "ENGINE_VERSION": "/engine_version",
            "STATE_HASH": "/state_sha256",
        }.get(self.reason)
        if expected is not None and self.path != expected:
            raise ValueError("checkpoint reason and path must agree")
        if self.reason == "STATE_INVARIANT" and not self.path.startswith("/state"):
            raise ValueError("state invariant path must be under /state")
        if self.reason == "CHECKPOINT_CONFIG_HASH" and self.path not in {
            "/config_sha256",
            "/state/config_snapshot",
        }:
            raise ValueError("config hash mismatch path is invalid")
        return self


class EventOutOfOrderDetails(FrozenModel):
    kind: Literal["event_out_of_order"]
    previous_start_at: datetime
    previous_end_at: datetime
    previous_recorded_at: datetime
    input_start_at: datetime
    input_end_at: datetime
    input_recorded_at: datetime

    @model_validator(mode="after")
    def validate_regression(self) -> EventOutOfOrderDetails:
        if not (
            self.input_recorded_at < self.previous_recorded_at
            or self.input_start_at < self.previous_end_at
        ):
            raise ValueError("input must regress or overlap the cursor")
        return self


class DuplicateConflictDetails(FrozenModel):
    kind: Literal["duplicate_conflict"]
    logical_start_at: datetime
    logical_end_at: datetime
    previous_input_sha256: Sha256
    input_sha256: Sha256

    @model_validator(mode="after")
    def validate_conflict(self) -> DuplicateConflictDetails:
        if self.logical_end_at < self.logical_start_at:
            raise ValueError("logical interval cannot be negative")
        if self.previous_input_sha256 == self.input_sha256:
            raise ValueError("conflicting input hashes must differ")
        return self


class RunFinishedDetails(FrozenModel):
    kind: Literal["run_finished"]
    status: Literal["FINISHED", "STOPPED", "BLOCKED_UNCLOSED", "BLOCKED_EXPIRY_UNRESOLVED"]
    finished_reason: Literal[
        "MARK_OPEN",
        "FORCE_CLOSED",
        "CONTRACT_CLOSED",
        "BLOCKED_UNCLOSED",
        "BLOCKED_EXPIRY_UNRESOLVED",
    ]

    @model_validator(mode="after")
    def validate_pair(self) -> RunFinishedDetails:
        expected = {
            "FINISHED": {"MARK_OPEN", "FORCE_CLOSED"},
            "STOPPED": {"CONTRACT_CLOSED"},
            "BLOCKED_UNCLOSED": {"BLOCKED_UNCLOSED"},
            "BLOCKED_EXPIRY_UNRESOLVED": {"BLOCKED_EXPIRY_UNRESOLVED"},
        }
        if self.finished_reason not in expected[self.status]:
            raise ValueError("terminal status and reason must agree")
        return self


EngineErrorDetails = Annotated[
    ValidationErrorDetails
    | InvalidBarEventDetails
    | EventAfterEndDetails
    | BatchLimitExceededDetails
    | UnsupportedConfigurationDetails
    | UnsupportedFillIntervalDetails
    | UnsupportedContractChangeDetails
    | ConfigMismatchDetails
    | CheckpointMismatchDetails
    | EventOutOfOrderDetails
    | DuplicateConflictDetails
    | RunFinishedDetails,
    Field(discriminator="kind"),
]

_MESSAGES: dict[str, str] = {
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
_KINDS = {code.lower(): code for code in _MESSAGES}


class EngineError(FrozenModel):
    code: EngineErrorCode
    message: Literal[
        "Engine input is invalid.",
        "Bar event is invalid.",
        "Bar event is after the run end.",
        "Engine batch bar limit is exceeded.",
        "Engine configuration is unsupported.",
        "Fill interval is unsupported.",
        "Contract change is unsupported.",
        "Engine configuration does not match state.",
        "Engine checkpoint is invalid.",
        "Engine event is out of order.",
        "Logical bar has conflicting content.",
        "Engine run is already terminal.",
    ]
    details: EngineErrorDetails

    @model_validator(mode="after")
    def validate_mapping(self) -> EngineError:
        if self.message != _MESSAGES[self.code] or _KINDS.get(self.details.kind) != self.code:
            raise ValueError("engine error code, message, and details kind must agree")
        return self


class EngineFailure(Exception):
    def __init__(self, error: EngineError) -> None:
        super().__init__(error.message)
        self.error = error
