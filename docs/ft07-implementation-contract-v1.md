# FT-07 deterministic decision and paper-fill engine implementation contract v1

Status: CONTRACT_CANDIDATE_FOR_IDENTICAL_BYTE_REVIEW.

This is an additive implementation binding for FT-07 only. It changes no frozen
FT-02 member and commissions no application implementation by itself. It contains
no durable job, lane repository, API, UI, MCP, provider, deployment, purchase,
exchange, broker-adapter, production-checkpoint, or live-order behavior. FT-08 and
later issue behavior is outside this packet.

## 1. Evidence, dependencies, and precedence

The accepted implementation base is
`865a23767c2dce26d33692e55a3c4bdefc17f18e` on fetched `origin/main`.
The following commits are ancestors of that base:

- FT-06 implementation head `e5596c9672bf50346d0d6b2a7290db1bf627bb5b`,
  merged by PR 23 as the accepted base commit. FT-06 issue 7 is closed.
- FT-05 implementation head `66e5e30d2483965fd9b6fac0e10ab180d8a5944c`,
  merged by PR 22 at `3c8132f7e7016175853f59d6c6534faec3c63e2b`.
- FT-02 contract commit `5aa4e7766846e889d45ac87c5da80db796869017`.

FT-05 issue 6 remains open even though PR 22 is merged and its owner-scoped
calendar, contract, bar, aggregation, revision, archive, and reader implementation
is integrated with green head CI. The common gate requires integrated dependency
code and exact symbol binding; it does not say an otherwise integrated dependency
is absent because its issue ledger was not closed. The open issue is therefore an
audited ledger inconsistency, not an FT-07 code-dependency blocker. This packet does
not close, edit, or silently waive issue 6. Any distinct unfinished FT-05 external
acceptance would remain FT-05 work and cannot be claimed by synthetic FT-07 tests.

At authoring time there is no open pull request and no other FT-07 branch/worktree.
The repository-root checkout is an unborn dirty prototype and is not an accepted
baseline; it remains untouched. Integrated FT-04, FT-05, and FT-06 worktrees and
remote branches do not authorize edits outside this FT-07 worktree.

The immutable FT-02 packet has 37 ordered manifest members. Every member hash and
byte length was recomputed at this base and the ordered aggregate is exactly
`9bb0e22bb72ccc0dc1db56ddcb8ba9106b5260f92710b2597072247c3f5f723d`.
Those 37 files must still match before implementation and after integration.

Normative precedence is:

1. `docs/contracts-v1.md` sections 3-7, section 10 only for the pure state and
   checkpoint payload at lines 736-768, and section 13.
2. The FT-07 recipe in `docs/issue-recipes-v1.md` and live issue 8.
3. The accepted `docs/strategies/configurability-v1.md` override.
4. The accepted final FT-06 public definition contract and amendment, and their
   integrated implementation at the base above.
5. This document, only where those sources leave an implementation binding open.
6. The Reversal-Breakout draft and its twelve examples as provenance, under every
   explicit FT-02 and configurability override above it.

If a higher-precedence source conflicts with this packet, implementation returns
`WAIT_CONTRACT_GAP`. The public callables, deterministic identifiers, canonical
checkpoint, first-slice currency/fill-interval caps, and finite runtime reason-code
inventory below bind previously unspecified implementation details. They do not
change a frozen formula, fill rule, risk rule, strategy value, or fixture byte.

## 2. Integrated symbols and exact owned public surface

FT-07 consumes, and never redefines, these integrated FT-05 symbols:

- `familytrade.market_data.models.CompletedBar`, `FuturesContract`,
  `DatasetRevision`, `CalendarVersion`, `BarSelection`, `CorrectionObservation`,
  `AggregatedFullBar`, `AggregateGap`, `AggregateRequest`, and `AggregateResult`.
- `familytrade.market_data.aggregate.aggregate_completed_bars` for conformance
  checks of every supported 5/15/30/60-minute derived bucket.
- Materialized `CalendarVersion.windows` as the sole runtime session, maintenance,
  scheduled-closure, trading-day, and DST authority.

FT-07 consumes, and never redefines, these integrated FT-06 symbols:

- `familytrade.strategies.definitions.StrategyVersion`, `RuleDefinition`, every
  `RuleNode`, `FeatureInstance`, `OrderPolicy`, `EntryWindow`, every `StopSpec` and
  `TargetSpec`, and every `SetupModule` variant.
- Only `StrategyVersion(status="validated")` with an exact recomputed canonical
  definition hash is executable. FT-07 does not parse, repair, or revalidate raw
  strategy mappings and does not mutate strategy persistence.

The new public runtime records live in
`src/familytrade/simulation/state.py`. They are strict frozen Pydantic v2 models,
forbid unknown fields, hide submitted values in validation errors, reject bool as
integer, require aware UTC datetimes, and serialize `Decimal` values as canonical
base-10 strings. The public records are:

- `CostModel`, `FillModel`, `FixedContractsSizing`, `StopRiskFractionSizing`,
  `RiskPolicy`, and their tagged unions.
- `EngineConfig`, `EmissionContext`, `CompletedBarEvent`, `FinishRunEvent`, and
  `EngineInputEvent`.
- `FeatureValue`, `FeatureRuntimeState`, `RuleResult`, `IntervalBucket`,
  `ZoneState`, `SetupSnapshot`, `SetupState`, `OrderState`, `PositionState`,
  `RiskState`, `EngineState`, and `EngineCheckpoint`.
- `OrderIntent`, `Decision`, `Fill`, `RunEvent`, `EngineStepResult`, and
  `EngineRunResult`.
- `EngineErrorCode` and `EngineError`.

`src/familytrade/simulation/engine.py` exports exactly:

~~~text
def initialize_engine(config: EngineConfig) -> EngineState

def step_engine(
    config: EngineConfig,
    state: EngineState,
    event: EngineInputEvent,
) -> EngineStepResult

def run_engine(
    config: EngineConfig,
    events: tuple[EngineInputEvent, ...],
    *,
    initial_state: EngineState | None = None,
) -> EngineRunResult

def checkpoint_engine(config: EngineConfig, state: EngineState) -> EngineCheckpoint

def restore_engine(
    config: EngineConfig,
    checkpoint: EngineCheckpoint,
) -> EngineState
~~~

These callables perform no I/O, database access, filesystem access, network call,
environment read, logging, random generation, or implicit clock read. All output is
a function of the typed config, prior state, and ordered input. Private helper names
are routine and are not public compatibility promises.

### 2.1 Exact runtime record shapes

The reusable value/evidence records are:

~~~text
FeatureValue = {
  feature_id, interval_seconds, evaluation_bar_end,
  value_type, unit, value:Decimal|int|bool|string|null,
  status:"KNOWN"|"UNKNOWN", reason_code:string|null,
  source_bar_record_ids:tuple[string,...], known_at:UTC timestamp
}

FeatureRuntimeState = {
  feature_id, recurrent_value:Decimal|null, consecutive_bars:int,
  history:tuple[FeatureValue,...], session_trading_day:date|null
}

RuleResult = {
  node_id, result:"PASS"|"FAIL"|"UNKNOWN",
  value:Decimal|int|bool|string|null, value_type, unit,
  source_bar_record_ids:tuple[string,...], known_at:UTC timestamp,
  reason_code:string
}

IntervalBucket = {
  interval_seconds, start_at, end_at,
  selections:tuple[BarSelection,...],
  published_base_revision_ids:tuple[lowercase UUIDv7|null,...],
  expected_component_count:int,
  status:"building"|"complete"|"broken"
}

ZoneState = {
  zone_id, kind:"support"|"resistance", low, high,
  creation_sequence:int, touch_count:int,
  created_zone_index:int, last_touch_zone_index:int,
  last_filled_execution_index:int|null,
  pivot_bar_end, confirmation_bar_end, known_at,
  source_bar_record_ids:tuple[string,...]
}

SetupSnapshot = {
  setup_id, family:"reversal"|"breakout", side:"long"|"short",
  source_zone_ids:tuple[string,...], arm_execution_index:int|null,
  signal_execution_index:int|null, unit, entry, stop, target,
  target_mode, frozen_feature_values:tuple[FeatureValue,...],
  source_bar_record_ids:tuple[string,...], known_at
}

SetupState = {
  module_kind, status:"IDLE"|"ARMED"|"ENTRY_PENDING"|"POSITION_OPEN"|
    "EXPIRED"|"CANCELLED", snapshot:SetupSnapshot|null,
  expires_after_execution_index:int|null, reason_code:string|null
}
~~~

`OrderIntent` is exactly the frozen section 13.3 record:

~~~text
{
  order_id, kind:"entry"|"close", order_type:"market"|"limit",
  side:"buy"|"sell", effect:"open"|"close", quantity:int,
  raw_price:Decimal|null, executable_price:Decimal|null,
  raw_stop:Decimal|null, stop_price:Decimal|null,
  raw_target:Decimal|null, target_price:Decimal|null,
  bracket_template:null|{stop:StopSpec,target:TargetSpec,
    frozen_feature_values:object},
  effective_at, submitted_at, active_from, expires_at,
  entry_bar_exit_policy, both_hit_policy
}
~~~

The stateful order and accounting records are:

~~~text
OrderState = {
  intent:OrderIntent,
  status:"PENDING"|"ACTIVE"|"FILLED"|"EXPIRED"|"CANCELLED",
  status_reason:string|null, activated_at:UTC timestamp|null,
  fill_sequence:int, source_setup_id:string|null
}

PositionState = {
  side:"long"|"short", quantity:int, contract_id,
  entry_order_id, entry_fill_id, entry_price,
  opened_at, opened_trading_day, entry_commission,
  protective_stop_order:OrderState, protective_target_order:OrderState
}

RiskState = {
  trading_day:date|null, daily_start_equity,
  filled_entries_in_trading_day:int,
  latches:tuple["DAILY_LOSS_LIMIT"|"CUMULATIVE_DRAWDOWN_LIMIT",...],
  high_water, current_drawdown, maximum_drawdown
}
~~~

`Decision`, `Fill`, and `RunEvent` use these exact frozen projections:

~~~text
Decision = {
  schema_version:"v1", decision_id, owner_user_id, run_id, lane_id,
  strategy_version_id, dataset_revision_id, source_bar_record_ids,
  execution_bar_end, decision_sequence, decision_type, side,
  setup_id, reason_code, evidence, order_intent,
  pre_state_sha256, post_state_sha256, causation_event_id,
  effective_at, decided_at, idempotency_key,
  created_at:decided_at, record_version:1
}

Fill = {
  schema_version:"v1", fill_id, owner_user_id, run_id, lane_id,
  order_id, contract_id, bar_record_id, fill_sequence,
  side:"buy"|"sell", effect:"open"|"close", quantity:int,
  base_price, fill_price, slippage, commission, currency:"USD",
  model_time, realized_pnl, cash_after, position_quantity_after,
  position_average_after, reason, causation_decision_id,
  created_at, fencing_token:positive int, record_version:1
}

RunEvent = {
  schema_version:"v1", run_event_id, owner_user_id,
  aggregate_type:"run"|"lane", aggregate_id, sequence,
  event_type, effective_at, recorded_at, payload,
  causation_id, correlation_id, state_sha256,
  attempt_id:lowercase UUIDv7|null, fencing_token:positive int,
  created_at:recorded_at, record_version:1
}
~~~

`bracket_template.frozen_feature_values` is a canonical object whose keys are
lexical feature IDs and whose values are the corresponding `FeatureValue` records.
The implicit section 3 persisted-entity fields are explicit here: all three records
have `schema_version:"v1"`, `owner_user_id`, `created_at`, and
`record_version:1`; domain record times determine `created_at` as shown rather than
an implicit clock.
Nullable fields are nullable only where the frozen contract says `|null`.
`Decision.evidence` is the frozen
`{evaluation_stage,results:tuple[RuleResult,...],selected_setup:SetupSnapshot|null}`
projection plus the required `historical|contemporaneous|reconstructed_after_outage`
evidence label. The result tuple is lexical by node ID. `idempotency_key` is the
deterministic UUIDv7 derived for the decision identity; it is not caller-supplied.

`EngineState` is exactly:

~~~text
{
  schema_version:"v1", engine_version:"paper-engine-v1", config_sha256,
  status:"ACTIVE"|"FINISHED"|"STOPPED"|"BLOCKED_UNCLOSED"|
    "BLOCKED_EXPIRY_UNRESOLVED",
  last_bar_start_at:null|UTC timestamp, last_bar_record_id:string|null,
  last_bar_payload_hash:string|null, last_availability_at:UTC timestamp|null,
  last_recorded_at:UTC timestamp|null, last_input_sha256:string|null,
  last_published_base_revision_id:lowercase UUIDv7|null,
  interval_buckets:tuple[IntervalBucket,...],
  feature_values:tuple[FeatureValue,...],
  feature_runtime:tuple[FeatureRuntimeState,...],
  zones:tuple[ZoneState,...], setups:tuple[SetupState,...],
  pending_entry:OrderState|null, position:PositionState|null,
  close_intent:OrderState|null,
  cash, equity, fees, realized_pnl, unrealized_pnl,
  exposure_seconds:int, last_mark:Decimal|null,
  last_mark_bar_record_id:string|null,
  mark_status:"none"|"fresh"|"stale",
  risk:RiskState,
  next_decision_sequence:int, next_fill_sequence:int,
  next_event_sequence:int, finished_reason:string|null
}
~~~

Feature runtime sorts by lexical feature ID. History is bounded to the validated
feature/node need plus one prior value for temporal comparisons. No unordered set
or implementation object may enter state bytes. Zones sort by creation sequence
then ID; setup states sort by module kind; buckets and feature values sort by
interval/end/ID.

`EngineStepResult` is exactly
`{state,decisions:tuple[Decision,...],intents:tuple[OrderIntent,...],
fills:tuple[Fill,...],events:tuple[RunEvent,...],replayed:boolean}`.
`EngineRunResult` has the same four aggregate output tuples plus final `state` and
`checkpoint`; concatenating successful step deltas in order must equal the batch
tuples byte-for-byte.

## 3. Exact configuration and input event contract

`EngineConfig` is exactly:

~~~text
{
  schema_version:"v1", engine_version:"paper-engine-v1",
  run_id:lowercase UUIDv7, lane_id:lowercase UUIDv7|null,
  mode:"backtest"|"forward_paper", owner_user_id:lowercase UUIDv7,
  strategy_version:StrategyVersion, contract:FuturesContract,
  calendar:CalendarVersion, dataset_revision:DatasetRevision|null,
  source:string[1..80], price_basis:"trades",
  start_at:UTC timestamp, end_at:UTC timestamp|null,
  force_close_at:UTC timestamp|null,
  starting_cash:positive Decimal, base_currency:"USD",
  cost_model:CostModel, fill_model:FillModel,
  sizing_policy:FixedContractsSizing|StopRiskFractionSizing,
  risk_policy:RiskPolicy, end_policy:"mark_open"|"force_close",
  random_seed:null
}
~~~

The policy records exactly match the frozen section 13.3 shapes. `CostModel` is
`{commission_per_contract_per_side,currency:"USD",market_slippage_ticks,
stop_slippage_ticks,limit_slippage_ticks}` with the frozen numerical bounds.
`FillModel` is `{fill_interval_seconds,partial_fill_policy:"all_or_none",
both_hit_policy,entry_bar_exit_policy}`. Sizing is exactly fixed contracts or stop
risk fraction, and `RiskPolicy` has the five frozen fields including
`max_positions:1`.

All config owner IDs agree. The validated strategy definition/hash/catalogue,
contract ID/version, contract calendar ID/version, and supplied calendar ID/version
must agree. Contract tick size and multiplier are positive. Every executable price
must be an exact tick multiple. The base currency, contract currency, and cost
currency must all be `USD`; USD money quantizes to `0.01`. Any other currency or
currency mismatch is `UNSUPPORTED_CONFIGURATION`. FX conversion and a general
currency-minor-unit registry are not silently invented in the MGC first slice.

The strategy execution interval remains one of `300|900|1800|3600`. A v1 fill
interval is supported when it is at least 60 seconds, is a whole multiple of 60,
is no larger than the execution interval, and divides it. The engine derives it
from canonical one-minute bars. This supports validated 60, 120, 300, 900, 1800,
and 3600-second examples when divisible, while a validated sub-minute or non-minute
value fails `UNSUPPORTED_FILL_INTERVAL` before state creation. It does not weaken
FT-06 validation or claim sub-minute source data.

Backtest requires one owned, published `DatasetRevision`, a finite `end_at`, and
modeled bar availability at each bar end. Forward paper requires a null pinned
revision, null `end_at`, `end_policy:"mark_open"`, and causal-latest selections.
`force_close` is backtest-only and requires explicit `force_close_at`, the start of
the last selected valid fill bar whose end is at or before `end_at`. `mark_open`
requires null `force_close_at`. A caller cannot change config after initialization;
the canonical config fingerprint in state enforces that boundary.

`EmissionContext` is
`{attempt_id:lowercase UUIDv7|null,fencing_token:positive int}`. It is opaque output
metadata supplied by the caller. The pure engine does not claim, acquire, renew, or
validate a lease and does not persist the token. This closes the required Fill and
RunEvent fields without implementing FT-08/FT-10/FT-11 ownership behavior.

`CompletedBarEvent` is exactly:

~~~text
{
  kind:"completed_bar_v1", selection:BarSelection,
  published_base_revision_id:lowercase UUIDv7|null,
  recorded_at:UTC timestamp, emission_context:EmissionContext
}
~~~

Its bar must be a canonical 60-second FT-05 `CompletedBar` for the config owner,
source, price basis, and actual contract. `recorded_at >= selection.availability_at`.
For a pinned backtest, the selection is archive-origin, availability equals bar end,
and `published_base_revision_id` equals the pinned revision. For forward paper,
availability is `max(end_at,completed_at,received_at)` and the base revision may be
null while a newer active row is consumed. Exact `bar_record_id` values, not a
mutable latest label, are evidence.

Only quality `valid` enters indicators, setup evaluation, fills, or marks. An
`invalid`, `missing`, or `duplicate_conflict` event is rejected as
`INVALID_BAR_EVENT`; callers represent absent expected bars by the gap inferred from
the next event or finish boundary and never fabricate a zero-volume bar. A selected
bar not on tick, invalid OHLC, negative/non-integral volume, mismatched interval, or
noncausal availability is also `INVALID_BAR_EVENT` with no state change.

`FinishRunEvent` is exactly
`{kind:"finish_run_v1",effective_at:UTC timestamp,recorded_at:UTC timestamp,
emission_context:EmissionContext}`.
It is valid only for a backtest, exactly once, with `effective_at=config.end_at` and
`recorded_at>=effective_at`. It advances gaps, expiry, marking, and the declared end
policy but can never create a fill at or after `end_at`. No pause, resume, scheduler,
lease, job, recovery, broker, or production-lane control is an FT-07 input.

`initialize_engine` sets cash, equity, high-water, and the applicable stored
trading-day baseline to starting cash; zeros fees, P&L, exposure, drawdown,
counters, and sequences; and creates no position, order, setup, or feature value.
Canonical events whose bar end is at or before `config.start_at` are warm-up only:
they update buckets, indicators, pivots, zones, and session state but emit no entry,
exit, order, Fill, cash change, exposure, or trading decision. The first entry
evaluation is the first full execution boundary strictly after `start_at`. This is
the same rule for historical and forward modes and prevents warm-up replay from
creating stale orders.

## 4. Event order, causality, aggregation, and deterministic identity

Events are processed in increasing canonical bar start time and nondecreasing
recorded time. The state stores the last canonical start, bar record ID, payload
hash, selection availability, and input hash. An exact replay of the immediately
preceding input returns the unchanged state and `replayed:true`. A same logical bar
with different record/payload after the cursor is `DUPLICATE_CONFLICT`; any older
nonidentical input is `EVENT_OUT_OF_ORDER`. `run_engine` removes adjacent exact
duplicates before stepping. Incremental chunks may overlap by their last event, so
batch and incremental results are identical across a caller-boundary duplicate.
Arbitrary old-event lookup and durable deduplication belong to later persistence,
not this bounded checkpoint.

The engine identifies every expected canonical minute from materialized open
calendar segments. A jump across an expected open minute emits a data-gap event,
breaks every affected consecutive warm-up and incomplete aggregate, expires orders
by wall-clock boundary, and makes the last mark stale. Maintenance and
`scheduled_closed` windows emit no invented bars and do not themselves break a
completed pre-break history. No bar or aggregate may cross a calendar segment.

The engine derives fill, feature, zone, and execution buckets from canonical minute
bars. Buckets anchor at each stored open-segment start. A bucket is usable only when
every expected minute is present and valid. Scheduled short final buckets are
rejected for strategy and fill use even though FT-05 can expose them under an
explicit research partial policy. For target intervals supported by
`aggregate_completed_bars`, derived OHLCV, source ID order, gaps, and boundaries must
match that integrated function exactly. Other valid minute-multiple fill intervals
use the same first/max/min/last/sum and segment-anchor rule.

A derived bar's causal availability is the maximum availability of all its source
minutes. At execution-bar end `T`, zone bars newly complete at or before `T` are
processed oldest first, then features and setups are evaluated once for that full
execution bar. Backtest `effective_at=T`. Forward `effective_at` is the maximum
causal availability of every cited source; `decided_at` is the input `recorded_at`
and is never earlier. A resulting order activates on the first full configured fill
bar whose start is at or after modeled effective time in backtest, or at or after
the forward submitted/recorded time. A close-based signal therefore never fills in
its signal bar, and delayed delivery never reaches back into an already-open bar.
For forward evidence, a Decision uses null `dataset_revision_id` when any cited
selection is active or the cited minutes do not share one non-null published base;
otherwise it uses that shared published base. A backtest always uses its pinned
revision. The complete ordered source bar IDs remain authoritative in both modes.

Record IDs are deterministic lowercase UUIDv7 values without randomness. For a
record timestamp, take floor Unix milliseconds as the 48-bit UUIDv7 timestamp.
Compute SHA-256 of canonical UTF-8 JSON
`["paper-engine-v1",domain,run_id,lane_id,ordered_identity_fields]`; use its first
ten bytes as the remaining bits, overwrite the version nibble with 7 and the UUID
variant bits with binary 10, and format the resulting sixteen bytes as lowercase
UUID text. The ordered identity fields are the frozen uniqueness tuple plus the
causation ID. Domains are `decision`, `order`, `fill`, and `event`. This gives stable
IDs across batch, incremental, checkpoint restore, Windows, and Linux while keeping
the required UUIDv7 wire shape.

## 5. Feature and rule evaluation

`src/familytrade/strategies/indicators.py` implements only the accepted
feature-catalogue-v1 names and signatures already enforced by FT-06. It uses a local
Decimal context with precision 34, exponent range `-6143..6144`, round-half-even,
and no dependence on the process-global Decimal context. Each indicator output and
recurrent state is quantized to twelve decimal places after its formula step.
Overflow, nonfinite values, zero invalid denominators, or an inexact value outside
the supported exponent range yields `UNKNOWN`, never a float or stale fallback.

The exact SMA, EMA seed/recurrence, Wilder RSI, Wilder ATR, previous-N relative
volume, and session VWAP equations, warm-up counts, zero cases, and gap behavior are
section 5.2. Session VWAP resets at the stored trading-day session start and not at
maintenance breaks or UTC/local midnight. Prior-session levels use the immediately
preceding complete stored trading session. Rolling levels exclude the current bar.

Confirmed pivots use left-inclusive/right-strict plateau comparisons and become
available only after the right confirmation bar completes. A bar that confirms both
high and low processes high first. `swing_regime_v1` uses the last two confirmed
highs and lows and remains `unknown` for insufficient or equal evidence. Level touch
and cross use the exact tick tolerance and previous-inclusive/current-strict close
rules. Every value records value type, unit, source bar record IDs, and `known_at`.

`src/familytrade/strategies/rules.py` evaluates the integrated `RuleDefinition`
graph without `eval`, code generation, dynamic fields, or mutation. It implements
the closed FT-06 type/unit/operator matrix and three-valued truth tables. Division
by zero is `UNKNOWN`. Every entry, exit, and filter root requires `PASS`; `UNKNOWN`
does not reuse an older value and a supplementary exit root at `UNKNOWN` never
closes a position. Computational short-circuiting may occur, but Decision evidence
contains every reachable leaf in lexical node-ID order with its exact value,
source IDs, known-at time, and stable reason.

The runtime UNKNOWN reason inventory is exactly:

- `NOT_READY_WARMUP`, `DATA_GAP`, `ZERO_DENOMINATOR`, `NUMERIC_DOMAIN`.
- `NOT_READY_SESSION`, `NOT_READY_PRIOR_SESSION`, `NOT_READY_PIVOTS`,
  `NOT_READY_LEVEL`, `NOT_READY_ATR`, `NOT_READY_PEAK_WINDOW`.
- `RULE_CHILD_UNKNOWN` and `FILTER_UNKNOWN`.

Unknown code paths outside this list are implementation defects and cannot become
public reason strings ad hoc.

## 6. Zones, setups, targets, and configuration

`src/familytrade/strategies/zones.py` and `setups.py` implement the accepted
configurable strategy behavior. Presets are examples; side, enabled families,
filters, confirmation mode, setup expiry, entry TTL, target mode, and supported
parameters remain versioned configuration. Setup expiry and entry-order TTL are
independent. Generic rules-only definitions and supported non-preset combinations
must work; no template-only narrowing is allowed.

The Reversal-Breakout runtime rules are exactly the reviewed draft after the higher
precedence overrides:

- Process every completed zone bar in event order. Time-distinct equal pivots are
  distinct touches; identity includes contract, zone interval, kind, pivot end, and
  confirmation end. ATR-on uses the confirmation/current selected zone bar and
  never falls back; ATR-off uses exactly one price point.
- Search zones in ascending creation sequence. Merge the first eligible zone under
  the frozen distance/width calculation; preserve its ID/creation sequence, update
  bounds/touches/last touch, expire by zone-bar age, then retain the newest bounded
  set. An armed setup keeps its frozen snapshot after later merge or expiry.
- Nearest eligible zone selection uses smallest price distance, then newer creation
  sequence, then lexical ID. Cooldown and daily entries are consumed only by the
  atomic entry fill, not signal or order placement.
- Base reversal is proximity; directional approach is opt-in. Base breakout is
  already-beyond; strict cross is opt-in. A breakout retest must be on a later full
  execution bar, remains eligible through exactly its configured expiry bars, and
  expires before the following bar is evaluated.
- Derive all same-boundary candidates from one pre-state. Opposing arms produce
  `CONFLICT_OPPOSING_ARMS` and no arm. Opposing entries produce
  `CONFLICT_OPPOSING_SIDES` and no order. Same-side breakout beats reversal;
  same-family ties use smaller ATR-normalized entry distance, newer zone, then
  lexical setup ID. Filter rejection cancels its candidate/arm and forbids same-bar
  re-arm.
- Recent-peak stop includes the signal execution bar and requires the full contiguous
  configured window. Arm/signal snapshots freeze unit, zones, recent extreme, VWAP,
  features, entry, stop, and target. Later information never moves a resting order.

Target modes are the exact section 5.5 formulas. `next_zone` uses only a confirmed
zone known at signal time and has no fallback. `r_multiple` excludes fees and later
gap slippage from price R. `measured_move` uses its frozen origin and impulse. Apply
the role/side tick table exactly once. After rounding require long
`stop < entry < target` or short `target < entry < stop`; otherwise produce only
`NO_TARGET_ZONE`, `INVALID_TARGET_DISTANCE`, or `INVALID_BRACKET` as specified and
no order.

The engine intentionally differs from the source Pine behavior by processing every
completed lower-timeframe bar, later-only retests, causal pivot confirmation,
frozen setup values, atomic conflicts, fill-counted cooldown/daily cap, durable
one-order/one-position state, and future-event fill eligibility. It does not emulate
lower-timeframe sampling, same-bar Pine ordering, fill-triggered recalculation,
unsupported intrabar chronology, or profit/trade-list parity.

## 7. Orders, fills, and precedence

`src/familytrade/simulation/orders.py` owns raw/executable price resolution,
activation, absolute expiry, order state, and OCO cancellation.
`src/familytrade/simulation/fills.py` owns prospective and committed fill selection
for one complete configured fill bar. V1 has one all-or-none entry order, at most one
position, and one protective stop-market/target-limit bracket per run state.

Entry TTL is execution-bar wall-clock time. All full fill bars in the half-open
active interval are eligible; expiry occurs before a fill bar or decision at the
exclusive expiry boundary. Missing bars and scheduled closures do not extend it.
Entry-window close, pause/risk state inherited in config, entry cutoff, liquidation,
or run force-close cancels pending entries but never protective exits. FT-07 has no
operator pause/resume API.

For a position and bracket active before a bar starts, precedence is:

1. Resolve a bracket trigger already crossed by the opening gap.
2. Otherwise fill an active market close at the open with adverse market slippage
   and cancel the bracket.
3. Otherwise evaluate intrabar bracket touches and named both-hit policy.
4. Only while flat, evaluate the one eligible entry.

Buy/sell market, buy/sell limit, long/short stop, and long/short target fills use
the exact section 6.3 symmetric formulas. A limit never fills worse than its limit;
a target gap receives opening price improvement and no v1 slippage. Commission is
charged only on Fill. A bracket active before the bar and touched on both sides uses
the configured `stop_first` or `target_first` policy. A position opened at the bar
open activates its bracket for the remainder of that bar. An entry reached first
intrabar uses the configured conservative-stop-first or next-bar-only policy; the
conservative default may apply the stop, never grants a same-bar target, and the
alternative must preserve its limitation in evidence.

For a market entry with relative stop/target specs, compute the prospective actual
entry including slippage, resolve and tick-round the complete bracket atomically,
then apply geometry and risk before committing. For a limit entry, bracket prices
resolved at signal time never move after a favorable gap. A geometry inversion is
`ENTRY_GEOMETRY_GAP`; a fill-time cap excess is `RISK_GAP`. Both cancel the entry
with zero fills and zero commission. No unprotected position can commit.

## 8. Cash, marks, sizing, risk, sessions, expiry, and end state

`src/familytrade/simulation/risk.py` owns sizing, fees, cash, realized/unrealized
P&L, marks, high-water/drawdown, daily baselines/counters, and risk latches. Futures
entry never subtracts notional. Entry commission is deducted immediately. Close
P&L is `direction*(exit-entry)*multiplier*quantity`; exit commission is then
deducted. Intermediate arithmetic retains at least twelve decimal digits and USD
cash/equity/fees/P&L quantize round-half-even to `0.01` after each fill. Marks use
the latest valid completed fill-interval close causally known.

The equity/drawdown series includes starting equity and equity after all events plus
each valid fill-bar close mark, net of costs. High-water never resets. Drawdown is
floored at zero and is bar-close, not intrabar excursion. A missing expected open
bar retains the last price with `mark_status:"stale"`, emits its quality label, and
blocks new entries; it neither invents a mark nor suppresses an already-active
protective exit on a later valid bar.

Stop-risk sizing uses the exact frozen budget and slipped-stop formula, includes two
commissions, floors quantity, and then applies configured/account caps. Fixed size
is rejected if the same modeled loss exceeds the per-entry cap. Entry geometry,
prospective risk, one-position state, and the entry-counter increment are one pure
state transition. Rejection reason codes are exactly `RISK_SIZE_ZERO`,
`ENTRY_GEOMETRY_GAP`, `RISK_GAP`, `DAILY_LOSS_LIMIT`, `DAILY_ENTRY_LIMIT`, and
`CUMULATIVE_DRAWDOWN_LIMIT`.

At a stored trading-day boundary, the last marked prior-day equity becomes
`daily_start_equity`; daily loss and entry count reset, and session VWAP resets.
Position basis, cash, high-water, cumulative drawdown, bracket, setup-independent
order state, and open-position lifetime carry. Daily loss and cumulative drawdown
limits latch inclusively at `>=`; daily entries are allowed only while count is
strictly below its limit. Latches cancel entry/setup state but preserve exits. A
later recovery in equity does not clear a same-day daily-loss latch.

At `entry_cutoff_at`, cancel entries/setups and block new ones. At
`liquidation_start_at`, cancel entry state and create the close intent. It fills at
the next eligible fill bar under normal gap/precedence rules; no stale or closed
market fill is invented. After close, state is stopped for that actual contract.
If no eligible close exists before `last_trade_at`, terminal status is
`BLOCKED_EXPIRY_UNRESOLVED` with the position and last mark visible. A config,
checkpoint, or event naming a different contract ID or record version is
`UNSUPPORTED_CONTRACT_CHANGE`; the engine never stitches, rolls, or transfers a
position.

`mark_open` finishes with the actual open position, pending entry/setup, bracket,
cash, latest mark/status, realized and unrealized P&L visible. It creates no
synthetic exit. `force_close` blocks entries that could outlive `force_close_at`,
creates a close intent at that precomputed bar start, and must close on that bar
before end. If the selected bar is absent/invalid or the close cannot occur, status
is `BLOCKED_UNCLOSED`; it never backdates from end. Forward paper cannot force-close.

FT-07 returns the state needed by later analysis but does not implement FT-12
metrics. It must expose cash, equity, fees, realized/unrealized P&L, position,
high-water, current/maximum drawdown, filled-entry count, exposure seconds, last
mark/status, and terminal reason so later metrics cannot assume liquidation.

## 9. Decisions, fills, events, and evidence

`Decision`, `Fill`, and `RunEvent` contain every frozen field in contracts sections
3.8-3.10. `Decision.order_intent` is the exact section 13.3 tagged record. Every
Decision records the contemporaneous feature/rule/setup inputs, all lexical leaf
results, stable reason code, source bar IDs, availability/known-at times, pre/post
state hashes, causation event ID, modeled effective time, and recorded time.

Backtest decisions always name the pinned dataset revision. Forward decisions name
the event's published base revision when it covers all cited archived bars, else
null; exact source bar IDs remain authoritative. A later publication or correction
never patches prior bytes. A bar at or before the committed cursor with a different
record ID is rejected and cannot recompute cash, decisions, fills, or indicators.

The stable domain reason inventory is closed for v1:

- Evaluation/setup: `ENTRY_RULE_PASS`, `ENTRY_RULE_FAIL`, `EXIT_RULE_PASS`,
  `EXIT_RULE_FAIL`, `SETUP_SIGNAL`, `ARM_LONG`, `ARM_SHORT`,
  `ENTRY_INTENT_LONG`, `ENTRY_INTENT_SHORT`, `FILTER_FAIL`, `FILTER_UNKNOWN`,
  `NOT_APPROACHING`, `NOT_RETESTED`, `NO_ELIGIBLE_ZONE`, `COOLDOWN_ACTIVE`,
  `CONFLICT_OPPOSING_ARMS`, `CONFLICT_OPPOSING_SIDES`, `BREAKOUT_WINS`,
  `SETUP_EXPIRED`, `NO_TARGET_ZONE`, `INVALID_TARGET_DISTANCE`, and
  `INVALID_BRACKET`, plus the section 5 UNKNOWN codes.
- Order/cancellation: `ORDER_SUBMITTED`, `ORDER_ACTIVATED`, `ORDER_EXPIRED`,
  `ENTRY_WINDOW_CLOSED`, `ENTRY_CUTOFF`, `CONTRACT_LIQUIDATION`,
  `FORCE_CLOSE`, `RISK_SIZE_ZERO`, `ENTRY_GEOMETRY_GAP`, `RISK_GAP`,
  `DAILY_LOSS_LIMIT`, `DAILY_ENTRY_LIMIT`, and
  `CUMULATIVE_DRAWDOWN_LIMIT`.
- Fill: `MARKET_ENTRY`, `LIMIT_ENTRY_GAP`, `LIMIT_ENTRY_TOUCH`, `STOP_GAP`,
  `STOP_TOUCH`, `TARGET_GAP`, `TARGET_TOUCH`, `MARKET_CLOSE`,
  `BOTH_HIT_STOP_FIRST`, `BOTH_HIT_TARGET_FIRST`, and
  `ENTRY_BAR_CONSERVATIVE_STOP`.
- Run/data: `DATA_GAP`, `MARK_OPEN`, `FORCE_CLOSED`, `BLOCKED_UNCLOSED`,
  `BLOCKED_EXPIRY_UNRESOLVED`, and `CONTRACT_CLOSED`.

An implementation may combine these into a more specific evidence object, but it
may not emit a new public reason string or use prose as the sole reason without
returning to identical-byte contract review.

The FT-07 event payload kinds are the relevant strict subset:

- `DECISION_RECORDED`, `ORDER_STATE`, `FILL_RECORDED`, `MARK_RECORDED`, and
  `RISK_LATCHED` with the frozen section 13.4 payloads.
- `DATA_QUALITY` with `{kind,start_at,end_at,status:"missing"|"invalid"|
  "stale",reason,source_bar_record_ids}`.
- `RUN_FINISHED` with `{kind,status,end_policy,cash,equity,realized_pnl,
  unrealized_pnl,position_open,pending_entry,mark_status,reason|null}`.

Events are returned in exact processing order. Within one input: opening exit,
market close, intrabar exit, entry evaluation/fill, decisions/order transitions,
close mark, risk latch, then state/finish record. Each sequence increments once.
`attempt_id` and `fencing_token` are copied unchanged from `EmissionContext` into
each Fill/RunEvent. The correlation ID is a deterministic UUIDv7 in the `event`
domain for the input identity. A later issue may validate ownership and persist
these immutable records atomically but may not reinterpret them.

## 10. Pure state, checkpoint, replay, and failures

`EngineState` contains only deterministic engine state:

- Config fingerprint, status, last consumed logical minute/record/payload/input hash,
  exact source IDs needed by open aggregates/evidence, and next deterministic
  decision/fill/event sequences.
- In-progress interval buckets; quantized feature recurrence/history sufficient for
  the validated maximum lookback; confirmed pivots/zones; setup state and frozen
  candidate snapshots.
- Pending entry, position, protective bracket, close intent, cash, equity, marks,
  fees, realized/unrealized P&L, exposure, high-water/drawdown, trading-day baseline,
  counters, and risk latches.
- Finished/blocked status and last stable error/reason. It contains no credential,
  database handle, path, provider payload, clock, lease, fence, outbox, or job state.

Canonical state bytes use frozen section 13.1. `state_sha256` is SHA-256 of the
state projection excluding any surrounding hash field. Every step returns exact
pre/post hashes. `EngineCheckpoint` is exactly
`{schema_version:"v1",format_version:1,engine_version:"paper-engine-v1",
config_sha256,state, state_sha256}`. Restore recomputes all hashes, validates every
typed invariant, and returns the identical state. Unknown format/engine version is
`CHECKPOINT_MISMATCH`; a changed strategy hash, contract ID/version, calendar
ID/version, dataset binding, policy, owner, mode, or run ID is `CONFIG_MISMATCH`,
except contract identity/version changes use `UNSUPPORTED_CONTRACT_CHANGE`.

Checkpoint serialization and restore are pure payload behavior only. FT-07 creates
no production checkpoint row/file, migration, lease, fence, lock, transaction,
recovery worker, notification, or deployment compatibility promise. FT-11 and
FT-18 own those boundaries.

`EngineErrorCode` is closed to:

- `VALIDATION_ERROR`, `INVALID_BAR_EVENT`, `UNSUPPORTED_CONFIGURATION`,
  `UNSUPPORTED_FILL_INTERVAL`, `UNSUPPORTED_CONTRACT_CHANGE`.
- `CONFIG_MISMATCH`, `CHECKPOINT_MISMATCH`, `EVENT_OUT_OF_ORDER`,
  `DUPLICATE_CONFLICT`, and `RUN_FINISHED`.

The code, sole public message, and condition are:

| Code | Fixed message | Condition |
| --- | --- | --- |
| VALIDATION_ERROR | Engine input is invalid. | A typed cross-field invariant not assigned a narrower code fails. |
| INVALID_BAR_EVENT | Bar event is invalid. | Bar quality, identity, OHLCV, tick, interval, or causal time is ineligible. |
| UNSUPPORTED_CONFIGURATION | Engine configuration is unsupported. | Currency, strategy status/hash/catalogue, owner, dataset, calendar, mode, random seed, or policy is unsupported/inconsistent. |
| UNSUPPORTED_FILL_INTERVAL | Fill interval is unsupported. | It is sub-minute, not a whole minute, larger than execution, or does not divide execution. |
| UNSUPPORTED_CONTRACT_CHANGE | Contract change is unsupported. | Contract ID or record version differs from the initialized checkpoint/config. |
| CONFIG_MISMATCH | Engine configuration does not match state. | Any non-contract config fingerprint field differs. |
| CHECKPOINT_MISMATCH | Engine checkpoint is invalid. | Format/engine version, state hash, or typed checkpoint invariant fails. |
| EVENT_OUT_OF_ORDER | Engine event is out of order. | A non-replay event is at or before the consumed cursor or recorded time regresses. |
| DUPLICATE_CONFLICT | Logical bar has conflicting content. | The consumed logical identity reappears with a different record or payload. |
| RUN_FINISHED | Engine run is already terminal. | A nonidentical input is supplied after a finished/stopped/blocked terminal state. |

`EngineError` is `{code,message,details}`. Messages are fixed by code and never
include owner-private values or exception text. Details contain only JSON Pointer
paths, stable reason names, and public IDs already supplied to this pure call. Every
error leaves state byte-identical. Domain rejections such as no target, rule UNKNOWN,
risk gap, or order expiry are Decisions/events and state transitions, not exceptions.

## 11. Exact fixture and acceptance matrix

All expected values are read from immutable fixture bytes, never generated from a
prototype. `tests/engine/` must load and assert the complete named `expected`
projection for every FT-07 recipe case:

| Frozen contracts example | Exact test |
| --- | --- |
| indicator_warmup_equality_and_causal_catalogue | test_indicator_warmup_equality_and_causal_catalogue |
| confirmed_pivot_plateau_and_regime_availability | test_confirmed_pivot_plateau_and_regime_availability |
| long_entry_then_stop_gap | test_long_entry_then_stop_gap |
| short_entry_then_stop_gap | test_short_entry_then_stop_gap |
| long_target_gap_price_improvement | test_long_target_gap_price_improvement |
| short_target_gap_price_improvement | test_short_target_gap_price_improvement |
| both_hit_after_open_fill_is_stop_first | test_both_hit_after_open_fill_is_stop_first |
| intrabar_entry_with_stop_and_target_is_conservative_stop | test_intrabar_entry_with_stop_and_target_is_conservative_stop |
| fees_and_open_end_position_marked_not_liquidated | test_fees_and_open_end_position_marked_not_liquidated |
| daily_loss_with_overnight_carried_position | test_daily_loss_with_overnight_carried_position |
| entry_order_multi_bar_ttl_expiry | test_entry_order_multi_bar_ttl_expiry |
| market_entry_gap_rejected_by_fill_time_risk | test_market_entry_gap_rejected_by_fill_time_risk |
| contract_expiry_close_and_no_roll | test_contract_expiry_close_and_no_roll |
| forward_delayed_bar_availability_and_exit_rule | test_forward_delayed_bar_availability_and_exit_rule |
| exit_rule_unknown_does_not_close | test_exit_rule_unknown_does_not_close |
| next_zone_targets_long_short_and_missing | test_next_zone_targets_long_short_and_missing |
| r_multiple_targets_long_short | test_r_multiple_targets_long_short |
| favorable_limit_gaps_revalidate_bracket_both_sides | test_favorable_limit_gaps_revalidate_bracket_both_sides |
| generic_market_entry_resolves_relative_bracket | test_generic_market_entry_resolves_relative_bracket |

There is no frozen `r_multiple_targets_long_short_and_missing` case. The exact case
is `r_multiple_targets_long_short`; no renamed fixture or fabricated missing branch
is permitted.

The twelve provenance cases in
`docs/strategies/reversal-breakout-examples-v1.json` map exactly to:

- `test_reversal_breakout_long_breakout_levels_and_next_event`
- `test_reversal_breakout_short_breakout_levels_and_next_event`
- `test_reversal_rejected_when_not_approaching`
- `test_breakout_expiration_boundary`
- `test_pivot_confirmation_lag`
- `test_beyond_mode_is_not_strict_cross`
- `test_same_bar_break_and_retest_is_not_causal_entry`
- `test_opposing_breakout_arms_are_atomic_conflict`
- `test_entry_intent_filter_rejection_cancels_arm`
- `test_decision_atr_reference_missing_and_atr_off`
- `test_cooldown_and_daily_cap_are_fill_counted`
- `test_recent_peak_window_includes_signal_bar`

These tests assert signal/setup mathematics and causal eligibility under the frozen
overrides. Where the old draft says a fill outcome awaited FT-02, the final FT-02
fill table now supplies it; the old sentence cannot override final fill accounting.

The named boundary and replay tests are:

- `test_batch_incremental_and_checkpoint_replay_are_byte_identical`
- `test_incremental_overlap_replays_exact_last_event_once`
- `test_same_logical_bar_with_different_payload_is_duplicate_conflict`
- `test_out_of_order_event_changes_no_state`
- `test_deterministic_uuid7_ids_and_state_hashes_match_on_windows_and_linux_vectors`
- `test_derived_buckets_match_ft05_aggregation_and_reject_scheduled_partials`
- `test_missing_invalid_and_closed_intervals_have_distinct_quality_effects`
- `test_stored_calendar_dst_fold_gap_and_maintenance_never_invent_or_flatten`
- `test_forward_availability_uses_all_source_bars_and_never_backfills_a_fill`
- `test_every_rule_leaf_is_recorded_in_lexical_order_with_known_at_and_sources`
- `test_three_valued_group_arithmetic_and_comparison_matrix_is_exhaustive`
- `test_long_and_short_tick_rounding_matrix_is_exhaustive`
- `test_target_first_and_next_bar_only_alternatives_are_explicit_and_deterministic`
- `test_limit_market_ttl_gap_and_entry_bar_precedence_matrix_is_exhaustive`
- `test_fixed_and_stop_fraction_sizing_fee_and_fill_time_risk_matrix_is_exhaustive`
- `test_daily_loss_entry_and_cumulative_drawdown_boundaries_are_inclusive`
- `test_overnight_long_and_short_carry_preserves_basis_bracket_cash_and_marking`
- `test_trading_day_boundary_resets_only_daily_risk_and_session_vwap`
- `test_entry_window_close_cancels_entry_without_flattening_position`
- `test_mark_open_retains_open_position_pending_entry_and_separate_pnl`
- `test_force_close_precomputed_bar_and_blocked_unclosed_are_causal`
- `test_contract_identity_or_version_change_is_rejected_without_roll`
- `test_subminute_and_nonminute_fill_intervals_are_unsupported_before_state_creation`
- `test_checkpoint_rejects_corruption_config_change_and_unknown_format`
- `test_decision_fill_and_event_records_round_trip_canonical_bytes`
- `test_engine_error_code_messages_and_no_state_change_matrix_is_exhaustive`

The full acceptance is: completed-bar causality; both sides; gap and touch symmetry;
both-hit and entry-bar policies; fees/ticks/money; warm-up/equality/confirmation;
setup conflict/freeze/expiry; entry windows; DST/maintenance/overnight carry; daily
and cumulative risk; expiry/no roll; delayed forward availability; open-end state;
exact contemporaneous evidence; and batch/incremental/checkpoint identity. All data,
prices, fees, P&L, calendars, and outcomes remain explicitly synthetic.

## 12. Allowed paths, commands, CI, and exclusions

The complete implementation path allowlist is:

- `docs/ft07-implementation-contract-v1.md`, immutable after identical-byte PASS.
- `src/familytrade/strategies/indicators.py`.
- `src/familytrade/strategies/rules.py`.
- `src/familytrade/strategies/zones.py`.
- `src/familytrade/strategies/setups.py`.
- `src/familytrade/simulation/state.py`.
- `src/familytrade/simulation/orders.py`.
- `src/familytrade/simulation/fills.py`.
- `src/familytrade/simulation/risk.py`.
- `src/familytrade/simulation/engine.py`.
- `tests/engine/`.
- `.github/workflows/ci.yml`, only adding `uv run pytest tests/engine` after the
  existing strategy test in the current PostgreSQL market-data job.

No existing FT-05 or FT-06 source/model/validator/repository file is editable under
this packet. No migration, pyproject, lock file, API, frontend, access service,
archive/catalog reader, durable run/job/lane, metric/report, provider, network,
credential, deployment, purchase, account, or live-order path is allowed. No
FT-08 scheduling/job behavior and no FT-10/FT-11/FT-12/FT-13 successor behavior may
be pulled forward. A needed change outside the allowlist returns
`WAIT_CONTRACT_GAP` with the exact collision.

Run from the exact base. Focused engine tests require no database, filesystem,
network, provider, account, or clock. The regression command uses a disposable
PostgreSQL 16 database in `FAMILYTRADE_TEST_DATABASE_URL`:

~~~text
uv sync --frozen --all-groups
uv run pytest tests/engine
uv run pytest tests/bootstrap
uv run pytest tests/access tests/market_data tests/strategies/test_definitions.py tests/engine
uv run ruff check src/familytrade/strategies/indicators.py src/familytrade/strategies/rules.py src/familytrade/strategies/zones.py src/familytrade/strategies/setups.py src/familytrade/simulation tests/engine
uv run ruff format --check src/familytrade/strategies/indicators.py src/familytrade/strategies/rules.py src/familytrade/strategies/zones.py src/familytrade/strategies/setups.py src/familytrade/simulation tests/engine
uv run mypy src
~~~

`tests/bootstrap/test_planning_packet.py` must again report 37 members and exact
aggregate `9bb0e22bb72ccc0dc1db56ddcb8ba9106b5260f92710b2597072247c3f5f723d`.
The implementation diff must have zero changes to every frozen member and both
FT-06 contract documents. CI for the exact candidate head must include the existing
bootstrap/access/market-data/strategy checks plus the new focused engine command.

## 13. Dispatch terminal and human gate

All trading, causality, configuration, fill, accounting, session, overnight, risk,
expiry, evidence, and checkpoint choices required for this first-slice engine are
settled by the higher-precedence reviewed contracts and the routine bindings above.
No consequential product/trading choice remains reserved and there is no human gate
before identical-byte review. In particular, reviewers cannot substitute a new fill
policy, narrower preset, implicit session flatten, alternate risk basis, automatic
roll, sub-minute data model, currency conversion, or Pine parity.

Current readiness is `READY_FOR_IDENTICAL_BYTE_REVIEW`, not implementation-ready.
Fresh Reviewer A (`gpt-5.6-terra / medium`) and Reviewer B
(`gpt-5.6-sol / high`) must independently read the frozen sources, final FT-06
contract/evidence, integrated FT-05/FT-06 symbols, and this exact file and both PASS
identical bytes. Reviewer A focuses acceptance/fixtures/public surface; Reviewer B
focuses causality, long/short gap precedence, both-hit ambiguity, exact money/risk,
overnight/end state, checkpoint/replay, and successor boundaries. Any byte change
requires both reviews again.

After two identical-byte PASS results, FT-07 becomes `PLANNING_READY` for its
issue-prescribed `gpt-5.6-sol / medium` implementation worker on this base, subject
to a fresh collision/dependency/digest check. No review result grants deployment,
provider contact, purchase, account change, production data, real credential, live
order, merge beyond the commissioned queue, or FT-08 work.
