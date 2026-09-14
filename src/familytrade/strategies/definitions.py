"""Strict, persistence-neutral FT-06 strategy-definition records.

This module deliberately describes configuration only.  It contains no indicator,
order, fill, lane, or provider execution behaviour.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class ValidationIssueCode(StrEnum):
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    INVALID_TYPE = "INVALID_TYPE"
    REQUIRED = "REQUIRED"
    MUTUALLY_EXCLUSIVE = "MUTUALLY_EXCLUSIVE"
    INVALID_ENUM = "INVALID_ENUM"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    INVALID_DECIMAL = "INVALID_DECIMAL"
    NONFINITE = "NONFINITE"
    DUPLICATE_KEY = "DUPLICATE_KEY"
    DUPLICATE_ID = "DUPLICATE_ID"
    UNKNOWN_REFERENCE = "UNKNOWN_REFERENCE"
    CYCLE = "CYCLE"
    ROOT_NOT_BOOLEAN = "ROOT_NOT_BOOLEAN"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    UNSUPPORTED_FEATURE = "UNSUPPORTED_FEATURE"
    UNSUPPORTED_PARAMETER = "UNSUPPORTED_PARAMETER"
    INVALID_INTERVAL = "INVALID_INTERVAL"
    INTERVAL_NOT_DIVISIBLE = "INTERVAL_NOT_DIVISIBLE"
    REQUIRED_FOR_LIMIT = "REQUIRED_FOR_LIMIT"
    FORBIDDEN_FOR_MARKET = "FORBIDDEN_FOR_MARKET"
    INVALID_SIDE_ROOT = "INVALID_SIDE_ROOT"
    INVALID_MODULE_COMBINATION = "INVALID_MODULE_COMBINATION"
    INVALID_WINDOW = "INVALID_WINDOW"
    SIZE_LIMIT = "SIZE_LIMIT"
    ISSUE_LIMIT = "ISSUE_LIMIT"
    LOOKBACK_LIMIT = "LOOKBACK_LIMIT"
    NAME_MISMATCH = "NAME_MISMATCH"


class ValidationIssue(FrozenModel):
    path: str
    code: ValidationIssueCode
    message: str


class FeatureInstance(FrozenModel):
    feature_id: str = Field(min_length=1, max_length=120)
    kind: Literal["input", "indicator", "structure", "level"]
    name: str = Field(min_length=1, max_length=120)
    output_type: Literal[
        "decimal", "integer", "boolean", "price", "volume", "timestamp", "side", "regime", "level"
    ]
    unit: Literal[
        "scalar",
        "ratio",
        "ratio_0_100",
        "count",
        "boolean",
        "contract_price",
        "contract_volume",
        "utc_timestamp",
        "side",
        "regime",
    ]
    interval_seconds: int = Field(ge=60, le=3600)
    parameters: dict[str, object]


class FeatureNode(FrozenModel):
    kind: Literal["feature"]
    node_id: str = Field(min_length=1, max_length=120)
    feature_id: str = Field(min_length=1, max_length=120)
    offset: int = Field(ge=0, le=2000)


class ConstantNode(FrozenModel):
    kind: Literal["constant"]
    node_id: str = Field(min_length=1, max_length=120)
    value_type: Literal[
        "decimal", "integer", "boolean", "price", "volume", "timestamp", "side", "regime", "level"
    ]
    unit: Literal[
        "scalar",
        "ratio",
        "ratio_0_100",
        "count",
        "boolean",
        "contract_price",
        "contract_volume",
        "utc_timestamp",
        "side",
        "regime",
    ]
    value: object


class ArithmeticNode(FrozenModel):
    kind: Literal["arithmetic"]
    node_id: str = Field(min_length=1, max_length=120)
    op: Literal["add", "subtract", "multiply", "divide", "min", "max"]
    args: tuple[str, ...]
    result_type: Literal["decimal", "integer", "price", "volume", "level"]
    unit: Literal["scalar", "ratio", "ratio_0_100", "count", "contract_price", "contract_volume"]


class CompareNode(FrozenModel):
    kind: Literal["compare"]
    node_id: str = Field(min_length=1, max_length=120)
    op: Literal["lt", "lte", "eq", "gte", "gt", "within"]
    left: str = Field(min_length=1, max_length=120)
    right: str = Field(min_length=1, max_length=120)
    tolerance: str | None


class TemporalCompareNode(FrozenModel):
    kind: Literal["temporal_compare"]
    node_id: str = Field(min_length=1, max_length=120)
    op: Literal["crosses_above", "crosses_below"]
    left_feature: str = Field(min_length=1, max_length=120)
    right_feature: str = Field(min_length=1, max_length=120)


class GroupNode(FrozenModel):
    kind: Literal["group"]
    node_id: str = Field(min_length=1, max_length=120)
    op: Literal["all", "any", "none"]
    children: tuple[str, ...]


type RuleNode = Annotated[
    FeatureNode | ConstantNode | ArithmeticNode | CompareNode | TemporalCompareNode | GroupNode,
    Field(discriminator="kind"),
]


class EntryRules(FrozenModel):
    long_root: str | None
    short_root: str | None


class ExitRules(FrozenModel):
    long_root: str | None
    short_root: str | None


class FeaturePriceSource(FrozenModel):
    kind: Literal["feature"]
    feature_id: str = Field(min_length=1, max_length=120)
    offset_ticks: int = Field(ge=-100_000, le=100_000)


class SetupPriceSource(FrozenModel):
    kind: Literal["setup_price"]
    field: Literal["entry"]


type PriceSource = Annotated[FeaturePriceSource | SetupPriceSource, Field(discriminator="kind")]


class SetupStop(FrozenModel):
    kind: Literal["setup_price"]
    field: Literal["stop"]


class FixedTicksStop(FrozenModel):
    kind: Literal["fixed_ticks"]
    ticks: int = Field(ge=1, le=100_000)


class AtrMultipleStop(FrozenModel):
    kind: Literal["atr_multiple"]
    feature_id: str = Field(min_length=1, max_length=120)
    multiple: str


type StopSpec = Annotated[SetupStop | FixedTicksStop | AtrMultipleStop, Field(discriminator="kind")]


class SetupTarget(FrozenModel):
    kind: Literal["setup_price"]
    field: Literal["target"]


class FixedTicksTarget(FrozenModel):
    kind: Literal["fixed_ticks"]
    ticks: int = Field(ge=1, le=100_000)


class RiskMultipleTarget(FrozenModel):
    kind: Literal["risk_multiple"]
    multiple: str


type TargetSpec = Annotated[
    SetupTarget | FixedTicksTarget | RiskMultipleTarget, Field(discriminator="kind")
]


class OrderPolicy(FrozenModel):
    entry_type: Literal["market", "limit"]
    limit_price_source: PriceSource | None
    entry_ttl_execution_bars: int = Field(ge=1, le=1000)
    both_hit_policy: Literal["stop_first", "target_first"]
    entry_bar_exit_policy: Literal["conservative_stop_first", "next_bar_only"]


class OnePositionSetup(FrozenModel):
    kind: Literal["one_position_v1"]


class ConfirmedPivotZonesSetup(FrozenModel):
    kind: Literal["confirmed_pivot_zones_v1"]
    zone_interval_seconds: int
    use_atr: bool
    atr_length: int = Field(ge=1, le=500)
    pivot_left: int = Field(ge=1, le=50)
    pivot_right: int = Field(ge=1, le=50)
    merge_multiple: str
    max_width_multiple: str
    minimum_touches: int = Field(ge=1, le=100_000)
    max_zones: int = Field(ge=1, le=1000)
    zone_max_age_bars: int = Field(ge=1, le=100_000)
    cooldown_execution_bars: int = Field(ge=0, le=100_000)


class ReversalSetup(FrozenModel):
    kind: Literal["reversal_setup_v1"]
    enabled: bool
    sides: Literal["long", "short", "both"]
    approach_multiple: str
    require_directional_approach: bool
    stop_buffer_multiple: str
    recent_peak_stop: bool
    peak_lookback: int = Field(ge=1, le=2000)
    target_mode: Literal["measured_move", "next_zone", "r_multiple"]
    measured_move_multiple: str
    r_multiple: str
    filter_root: str | None


class BreakoutRetestSetup(FrozenModel):
    kind: Literal["breakout_retest_v1"]
    enabled: bool
    sides: Literal["long", "short", "both"]
    confirmation_mode: Literal["beyond", "strict_cross"]
    break_multiple: str
    pullback_multiple: str
    setup_expiry_execution_bars: int = Field(ge=1, le=1000)
    stop_buffer_multiple: str
    use_vwap_stop: bool
    target_mode: Literal["measured_move", "next_zone", "r_multiple"]
    measured_move_multiple: str
    r_multiple: str
    arm_filter_root: str | None
    entry_filter_root: str | None


type SetupModule = Annotated[
    OnePositionSetup | ConfirmedPivotZonesSetup | ReversalSetup | BreakoutRetestSetup,
    Field(discriminator="kind"),
]


class ExitPolicy(FrozenModel):
    kind: Literal["bracket_exit_v1"]
    stop: StopSpec
    target: TargetSpec


class EntryWindow(FrozenModel):
    days_of_week: tuple[int, ...]
    start_local: str
    end_local: str
    calendar_id: str
    calendar_version: int = Field(ge=1)


class StrategyConstraints(FrozenModel):
    max_entries_per_trading_day: int = Field(ge=1, le=1000)
    entry_windows: tuple[EntryWindow, ...]


class RuleDefinition(FrozenModel):
    kind: Literal["rule_strategy_v1"]
    name: str = Field(min_length=1, max_length=120)
    side_policy: Literal["long", "short", "both"]
    features: tuple[FeatureInstance, ...]
    nodes: tuple[RuleNode, ...]
    entry_rules: EntryRules
    exit_rules: ExitRules
    entry_combination: Literal["rules_only", "setups_only", "setup_and_rules", "setup_or_rules"]
    exit_policy: ExitPolicy
    setup_modules: tuple[SetupModule, ...]
    order_policy: OrderPolicy
    constraints: StrategyConstraints


class StrategyDraftFromDefinitionInput(FrozenModel):
    kind: Literal["definition"]
    schema_version: Literal["v1"]
    name: str = Field(min_length=1, max_length=120)
    definition_schema_version: Literal["rule-strategy-v1"]
    definition: RuleDefinition
    catalogue_version: Literal["feature-catalogue-v1"]
    execution_interval_seconds: Literal[300, 900, 1800, 3600]
    fill_interval_seconds: int = 60


class StrategyDraftFromVersionInput(FrozenModel):
    kind: Literal["source_version"]
    schema_version: Literal["v1"]
    name: str = Field(min_length=1, max_length=120)
    source_version_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )


type StrategyDraftCreateInput = Annotated[
    StrategyDraftFromDefinitionInput | StrategyDraftFromVersionInput, Field(discriminator="kind")
]


class StrategyDraftEditInput(FrozenModel):
    schema_version: Literal["v1"]
    draft_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    expected_version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    definition_schema_version: Literal["rule-strategy-v1"]
    definition: RuleDefinition
    catalogue_version: Literal["feature-catalogue-v1"]
    execution_interval_seconds: Literal[300, 900, 1800, 3600]
    fill_interval_seconds: int


class StrategyDraftValidateInput(FrozenModel):
    schema_version: Literal["v1"]
    draft_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    expected_version: int = Field(ge=1)


class StrategyListInput(FrozenModel):
    schema_version: Literal["v1"]
    status: Literal["draft", "validated"] | None = None
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class StrategyVersion(FrozenModel):
    schema_version: Literal["v1"]
    owner_user_id: str
    strategy_version_id: str
    name: str
    status: Literal["draft", "validated"]
    definition_schema_version: Literal["rule-strategy-v1"]
    definition: RuleDefinition
    canonical_definition_sha256: str
    catalogue_version: Literal["feature-catalogue-v1"]
    execution_interval_seconds: int
    fill_interval_seconds: int
    required_warmup_bars: int
    created_from_version_id: str | None
    created_at: datetime
    record_version: int


class StrategyListItem(FrozenModel):
    schema_version: Literal["v1"]
    owner_user_id: str
    strategy_version_id: str
    name: str
    status: Literal["draft", "validated"]
    definition_schema_version: Literal["rule-strategy-v1"]
    canonical_definition_sha256: str
    catalogue_version: Literal["feature-catalogue-v1"]
    execution_interval_seconds: int
    fill_interval_seconds: int
    required_warmup_bars: int
    created_from_version_id: str | None
    created_at: datetime
    record_version: int


class StrategyPage(FrozenModel):
    schema_version: Literal["v1"]
    items: tuple[StrategyListItem, ...]
    next_cursor: str | None


class DefinitionValidationResult(FrozenModel):
    valid: bool
    definition: RuleDefinition | None
    errors: tuple[ValidationIssue, ...]
    canonical_definition_sha256: str | None
    required_warmup_bars: int | None


class StrategyValidationResult(FrozenModel):
    valid: bool
    strategy_version: StrategyVersion | None
    errors: tuple[ValidationIssue, ...]
