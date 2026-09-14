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
  `DatasetRevision`, `CalendarVersion`, `BarSelection`, `AggregatedFullBar`,
  `AggregateGap`, `AggregateRequest`, and `AggregateResult`.
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
- `EngineConfig`, `EmissionContext`, `CompletedBarEvent`, `DataQualityEvent`,
  `FinishRunEvent`, and `EngineInputEvent`.
- `SourceDatasetProvenance`, `FeatureValue`, `FeatureRuntimeState`, `RuleResult`,
  `IntervalBucket`, `AccumulatorPoint`, every `FeatureAccumulator`, `IntervalIndex`,
  `ZoneState`, `SetupSnapshot`, `SetupState`, `OrderState`,
  `ProtectiveOrderState`, `ProtectiveBracketState`, `PositionState`, `RiskState`,
  `EngineState`, and `EngineCheckpoint`.
- `OrderIntent`, `Decision`, `Fill`, `RunEvent`, `EngineStepResult`, and
  `EngineRunResult`, plus `FillEvidence` and `MarkEvidence` companions.
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
SourceDatasetProvenance = {
  bar_record_id:string,
  published_base_revision_id:lowercase UUIDv7|null
}

FeatureValue = {
  feature_id, interval_seconds, evaluation_bar_end,
  value_type, unit, value:Decimal|int|bool|string|null,
  status:"KNOWN"|"UNKNOWN", reason_code:string|null,
  source_bar_record_ids:tuple[string,...],
  source_dataset_provenance:tuple[SourceDatasetProvenance,...],
  known_at:UTC timestamp
}

AccumulatorPoint = {
  evaluation_bar_end:UTC timestamp, known_at:UTC timestamp, value:Decimal,
  source_bar_record_ids:tuple[string,...],
  source_dataset_provenance:tuple[SourceDatasetProvenance,...]
}

FeatureAccumulator = exactly one of {
  {kind:"none_v1"},
  {kind:"rolling_window_v1",points:tuple[AccumulatorPoint,...],sum:Decimal},
  {kind:"ema_v1",seed_points:tuple[AccumulatorPoint,...],seed_sum:Decimal,
    ema:Decimal|null},
  {kind:"rsi_wilder_v1",previous_close:AccumulatorPoint|null,
    delta_count:int,seed_gain_sum:Decimal,seed_loss_sum:Decimal,
    average_gain:Decimal|null,average_loss:Decimal|null},
  {kind:"atr_wilder_v1",previous_close:AccumulatorPoint|null,
    true_range_count:int,seed_true_range_sum:Decimal,
    average_true_range:Decimal|null},
  {kind:"relative_volume_v1",prior_volumes:tuple[AccumulatorPoint,...],
    prior_volume_sum:Decimal},
  {kind:"session_vwap_v1",trading_day:date|null,
    typical_volume_numerator:Decimal,volume_denominator:Decimal,
    source_bar_record_ids:tuple[string,...],
    source_dataset_provenance:tuple[SourceDatasetProvenance,...]},
  {kind:"pivot_v1",candidate_bars:tuple[BarSelection,...],
    candidate_source_dataset_provenance:tuple[SourceDatasetProvenance,...],
    confirmed_highs:tuple[AccumulatorPoint,...],
    confirmed_lows:tuple[AccumulatorPoint,...]},
  {kind:"session_level_v1",current_trading_day:date|null,
    current_high:Decimal|null,current_low:Decimal|null,current_complete:bool,
    previous_trading_day:date|null,previous_high:Decimal|null,
    previous_low:Decimal|null,previous_complete:bool,
    current_source_bar_record_ids:tuple[string,...],
    current_source_dataset_provenance:tuple[SourceDatasetProvenance,...],
    previous_source_bar_record_ids:tuple[string,...],
    previous_source_dataset_provenance:tuple[SourceDatasetProvenance,...]},
  {kind:"dependency_v1",dependency_feature_ids:tuple[string,...],
    previous_values:tuple[FeatureValue,...]}
}

FeatureRuntimeState = {
  feature_id, feature_name, consecutive_bars:int,
  accumulator:FeatureAccumulator,
  history:tuple[FeatureValue,...], session_trading_day:date|null,
  continuity_status:"clean"|"broken_until_reseed"|"broken_until_session"
}

RuleResult = {
  node_id, result:"PASS"|"FAIL"|"UNKNOWN",
  value:Decimal|bool|string|null, unit,
  source_bar_record_ids:tuple[string,...], known_at:UTC timestamp,
  reason_code:string
}

IntervalBucket = {
  interval_seconds, slot_index:int, start_at, end_at,
  selections:tuple[BarSelection,...],
  published_base_revision_ids:tuple[lowercase UUIDv7|null,...],
  expected_component_count:int,
  status:"building"|"complete"|"broken"
}

IntervalIndex = {interval_seconds:int,last_slot_index:int|null}

ZoneState = {
  zone_id:lowercase UUIDv7, kind:"support"|"resistance", low, high,
  creation_sequence:int, touch_count:int,
  created_zone_index:int, last_touch_zone_index:int,
  last_filled_execution_index:int|null,
  pivot_bar_end, confirmation_bar_end, known_at,
  source_bar_record_ids:tuple[string,...],
  source_dataset_provenance:tuple[SourceDatasetProvenance,...]
}

SetupSnapshot = {
  setup_id:lowercase UUIDv7, family:"reversal"|"breakout", side:"long"|"short",
  setup_sequence:int,
  source_zone_ids:tuple[lowercase UUIDv7,...], arm_execution_index:int|null,
  signal_execution_index:int|null, unit, entry, stop, target,
  target_mode, frozen_feature_values:tuple[FeatureValue,...],
  source_bar_record_ids:tuple[string,...],
  source_dataset_provenance:tuple[SourceDatasetProvenance,...], known_at
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
  order_sequence:int,
  status:"PENDING"|"ACTIVE"|"FILLED"|"EXPIRED"|"CANCELLED",
  status_reason:string|null, activated_at:UTC timestamp|null,
  fill_sequence:int|null, source_setup_id:string|null,
  originating_decision_id:lowercase UUIDv7|null
}

PositionState = {
  side:"long"|"short", quantity:int, contract_id,
  entry_order_id, entry_fill_id, entry_price,
  opened_at, opened_trading_day, entry_commission,
  protective_bracket:ProtectiveBracketState
}

ProtectiveOrderState = {
  order_id:lowercase UUIDv7,
  role:"protective_stop"|"profit_target",
  order_type:"stop_market"|"limit",
  side:"buy"|"sell", effect:"close", quantity:positive int,
  trigger_price:finite Decimal,
  status:"ACTIVE"|"FILLED"|"CANCELLED",
  active_from:UTC timestamp, filled_at:UTC timestamp|null,
  cancelled_at:UTC timestamp|null, status_reason:string|null,
  entry_order_id:lowercase UUIDv7, entry_fill_id:lowercase UUIDv7,
  order_sequence:int, fill_sequence:int|null,
  originating_decision_id:lowercase UUIDv7
}

ProtectiveBracketState = {
  stop:ProtectiveOrderState, target:ProtectiveOrderState,
  both_hit_policy:"stop_first"|"target_first",
  entry_bar_exit_policy:"conservative_stop_first"|"next_bar_only"
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
  execution_bar_end, decision_sequence,
  decision_type:"HOLD"|"ARM"|"ENTRY"|"CLOSE"|"CANCEL"|"REJECT",
  side:"long"|"short"|null,
  setup_id:lowercase UUIDv7|null, reason_code, evidence,
  order_intent:OrderIntent|null,
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
  position_average_after, reason,
  causation_decision_id:lowercase UUIDv7|null,
  created_at, fencing_token:positive int, record_version:1
}

FillEvidence = {
  fill_id:lowercase UUIDv7,
  fill_bar_start_at:UTC timestamp, fill_bar_end_at:UTC timestamp,
  bar_record_id_role:"availability_anchor",
  availability_anchor_bar_record_id:string,
  source_bar_record_ids:tuple[string,...],
  source_dataset_provenance:tuple[SourceDatasetProvenance,...]
}

MarkEvidence = {
  run_event_id:lowercase UUIDv7,
  fill_bar_start_at:UTC timestamp, fill_bar_end_at:UTC timestamp,
  close_bar_record_id:string,
  source_bar_record_ids:tuple[string,...],
  source_dataset_provenance:tuple[SourceDatasetProvenance,...]
}

RunEvent = {
  schema_version:"v1", run_event_id, owner_user_id,
  aggregate_type:"run"|"lane", aggregate_id:lowercase UUIDv7, sequence,
  event_type, effective_at, recorded_at, payload,
  causation_id, correlation_id, state_sha256,
  attempt_id:lowercase UUIDv7|null, fencing_token:positive int|null,
  created_at:recorded_at, record_version:1
}
~~~

`OrderState` is used only for frozen entry and market-close `OrderIntent` values.
A protective leg is never an `OrderIntent`: its executable boundary is
`trigger_price`, so the engine does not invent a market order with a non-null price
or extend the frozen order-intent union. A protective stop has
`role:"protective_stop",order_type:"stop_market"`; a target has
`role:"profit_target",order_type:"limit"`. Both close the entire position, use the
opposite side, share the entry identity and activation time, and serialize in the
fixed `stop,target` order. For a long, `stop.trigger_price < entry_price <
target.trigger_price`; for a short, the inequalities reverse. The bracket is
created atomically with an entry fill and has exactly one live leg of each role.
Filling either leg sets its fill fields and cancels the sibling in the same state
transition; cancellation never fabricates a Fill.
`trigger_price` is any finite signed Decimal that is an exact contract tick
multiple; zero and negative futures prices are not rejected merely for their sign.
Only the frozen side/role rounding and long/short geometry constrain it.

`bracket_template.frozen_feature_values` is a canonical object whose keys are
lexical feature IDs and whose values are the corresponding `FeatureValue` records.
The implicit section 3 persisted-entity fields are explicit here: all three records
have `schema_version:"v1"`, `owner_user_id`, `created_at`, and
`record_version:1`; domain record times determine `created_at` as shown rather than
an implicit clock.
The frozen public `RunEvent.fencing_token` is nullable. Its strict model must accept,
serialize, and round-trip null for existing schema-compatible event bytes. FT-07's
own engine emission is narrower: every `EmissionContext` has a positive token and
every newly emitted Fill or RunEvent copies that positive value, so the engine never
creates a null-token event. Null compatibility does not relax the FT-07 input
context or claim FT-11 fence validation.
Nullable fields are nullable only where the frozen contract says `|null`.
`Decision.evidence` is exactly
`{evaluation_stage:"entry_rule"|"exit_rule"|"arm"|"entry_intent",
evidence_mode:"historical"|"contemporaneous"|"reconstructed_after_outage",
results:tuple[RuleResult,...],selected_setup:SetupSnapshot|null}`. `evidence_mode`
is the single named key that realizes the otherwise unnamed required label in
frozen section 13.3; no other evidence metadata key is allowed. `RuleResult` has
exactly the frozen fields above: it has no `value_type`, and an integer-valued node
is projected as an exact integral Decimal (for example `Decimal("3")`), never a
JSON integer. The result tuple is lexical by node ID. `idempotency_key` is the
deterministic UUIDv7 derived for the decision identity; it is not caller-supplied.

Accumulator selection is closed by feature name: direct bar inputs use `none_v1`;
SMA and rolling high/low use `rolling_window_v1`; EMA, Wilder RSI, Wilder ATR,
relative volume, and session VWAP use their same-named variants; confirmed pivots
and swing regime use `pivot_v1`; prior-session high/low use `session_level_v1`;
level touch/cross use `dependency_v1`. Every configured length, ordered seed/window
point, previous close, quantized recurrent average, sum, numerator, denominator,
current/previous session completeness flag, pivot candidate, dependency previous
value, and source ID required for the next output is therefore present in canonical
state. Missing/invalid quality resets bounded rolling/Wilder/EMA seeds and marks
them `broken_until_reseed`; a broken session VWAP or prior-session accumulator is
`broken_until_session` until the next stored trading-day start. Checkpoint restore
does not recompute any accumulator from unavailable earlier inputs.

Every persisted FT-07 evidence record above other than the frozen `RuleResult` that
carries `source_bar_record_ids` also carries the parallel
`source_dataset_provenance` tuple (or the explicitly named current, previous, or
mark variant). The two tuples have identical length and order; element
`i` repeats source ID `i` and records the non-null published-base revision supplied
with that selected archived bar, or null for an active/unpublished source. Source
IDs are ordered by first causal use and are unique within a record. The same source
ID must have the same provenance everywhere in live state. `IntervalBucket`
similarly requires `selections` and `published_base_revision_ids` to have identical
length/order, with each revision belonging to the selection at that index.
`pivot_v1.candidate_source_dataset_provenance` likewise has one element per
`candidate_bars` selection in candidate order and repeats that selection's exact
record ID/revision pair.

Provenance is retained with its owning accumulator point, feature history value,
zone, setup snapshot, in-progress bucket, session accumulator, or last-mark
evidence; it is never inferred from `last_published_base_revision_id` and a later
base transition never rewrites it. There is no separate provenance archive. When
the already-bounded owning evidence is evicted, its companion provenance is evicted
atomically. Across all live records the number of distinct provenance elements is
therefore at most `canonical_bar_count`, itself at most the configured
`max_canonical_bars` and absolutely at most `2000000`; within a record it is exactly
the number of cited source IDs. Duplicate companion entries, missing entries,
changed mappings, or excess entries make a restored checkpoint
`CHECKPOINT_MISMATCH` and make a newly supplied conflicting input
`DUPLICATE_CONFLICT`, with no state change.

`RuleResult` remains byte-for-byte the frozen shape and gains no provenance field.
While a Decision is assembled, each RuleResult source ID must resolve through the
persisted feature/setup/zone/current-bucket provenance above. The Decision stores
the frozen RuleResult only after the complete cited-source reduction has succeeded.

`fill_sequence` is null for every pending, active, expired, or cancelled regular or
protective order. On a committed Fill, the order snapshot receives the current
global `next_fill_sequence`, the Fill uses that same integer, and state increments
`next_fill_sequence` by one. A sibling OCO cancellation retains null. Rejection,
cancellation, expiry, semantic replay, and prospective fill evaluation consume no fill
sequence.
`activated_at`, `filled_at`, and `cancelled_at` are modeled transition times derived
from the eligible bar/boundary, never persistence times. Durable record timing is
carried only by Decision/OrderIntent/Fill/RunEvent under the explicit section 4
rules.

`originating_decision_id` is FT-07-owned checkpointed causal state (and part of the
FT-07 protective snapshot), not a field added to any frozen FT-02 public model. An
entry `OrderState` stores the exact creating `ENTRY` Decision ID whether
the entry is rules-only (`source_setup_id:null`) or setup-backed. A supplementary
exit-rule close stores its exact creating `CLOSE` Decision ID. Contract-liquidation
and force-close lifecycle orders store null because those actions deliberately
create no Decision. Each protective record inherits the non-null originating entry
Decision ID from the filled entry order. No order UUID is decoded to recover this
value, and `source_setup_id` is never used as a substitute.

These are the only causal-retention locations. The exact `EngineState` topology
permits at most one field on each of `pending_entry`, `close_intent`, and
`scheduled_force_close`, plus one on each of the two protective legs: at most five
live values and no history or auxiliary map. Cancellation, expiry, or Fill retains
the value in that transition's internal order snapshot and emitted protective
snapshot, then removes it atomically when the existing live pointer/bracket is
cleared. Once a Fill has been emitted, the frozen Fill itself is the durable causal
projection and no completed-order causation archive remains in state.

No `originating_correlation_id` or originating pre-state hash is retained. The
frozen Fill asks only for `causation_decision_id`; the Fill/RunEvent correlation is
derived from the accepted fill-causing input, while the canonical current-state
hash already commits the retained Decision ID. Adding either extra value would be
redundant state rather than information required to reconstruct a frozen field.
Semantic replay never recreates or rebinds an order: it returns the existing state,
including these causal fields, byte-identically with empty deltas and no sequence
advance.

`EngineState` is exactly:

~~~text
{
  schema_version:"v1", engine_version:"paper-engine-v1", config_sha256,
  status:"ACTIVE"|"CLOSING"|"FINISHED"|"STOPPED"|"BLOCKED_UNCLOSED"|
    "BLOCKED_EXPIRY_UNRESOLVED",
  last_input_kind:null|"completed_bar_v1"|"data_quality_v1"|"finish_run_v1",
  last_input_start_at:null|UTC timestamp,
  last_input_end_at:null|UTC timestamp,
  last_bar_start_at:null|UTC timestamp, last_bar_record_id:string|null,
  last_bar_payload_hash:string|null, last_availability_at:UTC timestamp|null,
  last_recorded_at:UTC timestamp|null, last_input_sha256:string|null,
  last_published_base_revision_id:lowercase UUIDv7|null,
  canonical_bar_count:int,
  last_source_slot_index:int|null,
  last_fill_slot_index:int|null,
  last_execution_slot_index:int|null,
  zone_slot_indexes:tuple[IntervalIndex,...],
  interval_buckets:tuple[IntervalBucket,...],
  feature_values:tuple[FeatureValue,...],
  feature_runtime:tuple[FeatureRuntimeState,...],
  zones:tuple[ZoneState,...], setups:tuple[SetupState,...],
  pending_entry:OrderState|null, position:PositionState|null,
  close_intent:OrderState|null,
  scheduled_force_close:OrderState|null,
  cash, equity, fees, realized_pnl, unrealized_pnl,
  exposure_seconds:int, last_mark:Decimal|null,
  last_mark_bar_record_id:string|null,
  last_mark_source_bar_record_ids:tuple[string,...],
  last_mark_source_dataset_provenance:tuple[SourceDatasetProvenance,...],
  mark_status:"none"|"fresh"|"stale",
  risk:RiskState,
  next_decision_sequence:int, next_order_sequence:int,
  next_fill_sequence:int, next_event_sequence:int,
  next_zone_sequence:int, next_setup_sequence:int,
  next_correlation_sequence:int,
  force_close_state:"not_applicable"|"waiting"|"pending"|"completed"|"blocked",
  finished_reason:string|null
}
~~~

Feature runtime sorts by lexical feature ID. History is bounded to the validated
feature/node need plus one prior value for temporal comparisons. No unordered set
or implementation object may enter state bytes. Zones sort by creation sequence
then ID; setup states sort by module kind; buckets and feature values sort by
interval/end/ID.

All slot indexes begin at zero on the first represented scheduled slot and advance
once per subsequent expected slot, including explicit missing/invalid open slots.
`last_source_slot_index` is the canonical minute cursor;
`last_fill_slot_index` and `last_execution_slot_index` advance at their configured
full-bar boundaries even when the bucket is broken. `zone_slot_indexes` has one
entry for each enabled zone interval, sorted by interval. `IntervalBucket.slot_index`
is the index it will complete. Setup arm/signal/expiry and cooldown use only the
stored execution index; zone creation/touch/age use only the matching stored zone
index. These indexes, all in-progress buckets, and every accumulator above are
checkpointed and hashed, so chunking or restore cannot reset an expiry or cooldown.

`status:"CLOSING"` is the nonterminal status whenever `position != null` and
`close_intent != null`, whether the close was caused by an exit rule, liquidation,
or force close. A bracket fill atomically cancels that close intent. A normal
rule-driven close returns to `ACTIVE` while the run can continue; liquidation and
force-close use the terminal transitions in section 8. This includes the exact
`CLOSING` projection required at the frozen `at_liquidation_start` checkpoint.

`EngineStepResult` is exactly
`{state,decisions:tuple[Decision,...],intents:tuple[OrderIntent,...],
protective_orders:tuple[ProtectiveOrderState,...],fills:tuple[Fill,...],
fill_evidence:tuple[FillEvidence,...],mark_evidence:tuple[MarkEvidence,...],
events:tuple[RunEvent,...],replayed:boolean}`.
`EngineRunResult` has the same seven aggregate output tuples plus final `state` and
`checkpoint`; concatenating successful step deltas in order must equal the batch
tuples byte-for-byte.

`protective_orders` emits strict record snapshots rather than frozen order intents.
Creation emits stop then target. A protective fill emits the filled leg then the
cancelled sibling, while a close-intent fill emits cancelled stop then cancelled
target; within each same-action group stop precedes target. This output preserves
role, type, trigger, entry causation, originating entry Decision ID, and lifecycle
even after the live bracket is removed from terminal/current state.

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
  entry_windows:tuple[EntryWindow,...],
  start_at:UTC timestamp, end_at:UTC timestamp|null,
  force_close_at:UTC timestamp|null,
  starting_cash:positive Decimal, base_currency:"USD",
  cost_model:CostModel, fill_model:FillModel,
  sizing_policy:FixedContractsSizing|StopRiskFractionSizing,
  risk_policy:RiskPolicy, end_policy:"mark_open"|"force_close",
  max_canonical_bars:int=500000, random_seed:null
}
~~~

The policy records exactly match the frozen section 13.3 shapes. `CostModel` is
`{commission_per_contract_per_side,currency:"USD",market_slippage_ticks,
stop_slippage_ticks,limit_slippage_ticks}` with the frozen numerical bounds.
`FillModel` is `{fill_interval_seconds,partial_fill_policy:"all_or_none",
both_hit_policy,entry_bar_exit_policy}`. Sizing is exactly fixed contracts or stop
risk fraction, and `RiskPolicy` has the five frozen fields including
`max_positions:1`.

`EngineConfig.fill_model.fill_interval_seconds` must equal the immutable
`strategy_version.fill_interval_seconds` exactly. Run-level fill policy selects the
accepted execution semantics but cannot change the validated strategy version's
timeframe. A mismatch is `UNSUPPORTED_CONFIGURATION` during initialization before
state, bucket, or counter creation. On a later call, a changed config is the normal
`CONFIG_MISMATCH` against the state fingerprint. Divisibility and minimum-interval
validation still apply after equality; there is no second runtime authority that
may widen or narrow the strategy interval.

`entry_windows` is the run-level window constraint carried by frozen `RunSpec`.
The strategy-level constraint is
`strategy_version.definition.constraints.entry_windows`. Each tuple independently
uses the accepted strict `EntryWindow` shape and the supplied calendar's stored
IANA labels and UTC segments. An empty tuple at either level means no additional
narrowing at that level. A new entry is eligible only when its activation time is
in a stored open segment and is in at least one run window, when any, **and** at
least one strategy window, when any. Thus the effective window is their
intersection: run authority may narrow a strategy but cannot widen it. Windows are
not flattened, inferred from local wall-clock arithmetic, or allowed to move an
activation across a DST fold, gap, maintenance interval, or `scheduled_closed`
span. Window close cancels a pending entry and setup; it never closes an open
position or cancels protection.

Entry-window ordering follows the frozen opening-gap-before-intrabar model. An
entry is open-window eligible at fill-bar start only when that instant is in one
window from each non-empty run and strategy tuple. If it is already active then,
opening-gap evaluation occurs at `bar.start_at` and may fill because that modeled
instant is causally before any later window close. If it remains unfilled and an
effective window closes strictly inside the bar, cancel it at that exact boundary
before evaluating any aggregate OHLC intrabar touch, whose position relative to the
close is unknowable. A close exactly at bar start cancels before opening-gap
evaluation; a close exactly at bar end cancels before an intrabar touch modeled at
that end. An internal window open permits no retroactive gap/touch fill; an order
may activate only on a later fill-bar start that is in the effective window, subject
to unchanged TTL. Protection is never constrained by entry windows.

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
modeled bar availability at each bar end. Its half-open `[start_at,end_at)` span is
at most five calendar years: the maximum end is the same UTC month/day/time five
years later, with February 29 clipped to February 28 when the fifth year is not a
leap year. Equality is allowed; a larger span is `UNSUPPORTED_CONFIGURATION`
before state creation. Forward paper requires a null pinned
revision, null `end_at`, `end_policy:"mark_open"`, and causal-latest selections.

The backtest DatasetRevision cross-field projection is closed and uses the
integrated model names without aliases. It requires:

- `dataset_revision.owner_user_id == config.owner_user_id` and
  `dataset_revision.status == "published"` with non-null `published_at`.
- `dataset_revision.series_key.owner_user_id == config.owner_user_id`,
  `.source == config.source`, `.price_basis == config.price_basis`, and
  `.contract_id == config.contract.contract_id`.
- `dataset_revision.series_key.interval_seconds == 60`, the canonical FT-05 source
  interval consumed by this engine. It must divide
  `config.fill_model.fill_interval_seconds`, which separately equals
  `strategy_version.fill_interval_seconds`; an aggregate fill interval is never
  misrepresented as the archive series interval.
- `dataset_revision.contract_version == config.contract.record_version`.
- `dataset_revision.calendar_id == config.calendar.calendar_id ==
  config.contract.calendar_id` and `dataset_revision.calendar_version ==
  config.calendar.calendar_version == config.contract.calendar_version`.
- `config.calendar.coverage_start <= dataset_revision.coverage_start <
  dataset_revision.coverage_end <= config.calendar.coverage_end` and
  `dataset_revision.coverage_start <= config.start_at < config.end_at <=
  dataset_revision.coverage_end`. Coverage remains half-open; equality at the outer
  start/end is valid. Every accepted pinned warm-up or run `BarSelection` must also
  be wholly contained in dataset and calendar coverage and have the same owned
  series key and pinned revision ID.

A selected backtest may end early with `end_policy:"mark_open"` only when
`config.end_at < config.contract.liquidation_start_at`; that is the frozen research
snapshot behavior and may deliberately finish with an open position, pending entry,
or ordinary exit close visible. If `config.end_at >=
config.contract.liquidation_start_at`, the run reaches the actual-contract
liquidation lifecycle and initialization instead requires
`config.end_at >= config.contract.last_trade_at`,
`dataset_revision.coverage_end >= config.contract.last_trade_at`, and
`config.calendar.coverage_end >= config.contract.last_trade_at`. Because coverage
and trading are half-open, equality at `last_trade_at` covers the complete eligible
liquidation window while permitting no Fill at or after it. This rule applies to
both end policies and to a run whose `start_at` is already within the liquidation
window. It prevents an ordinary Finish boundary from stranding a lifecycle close
between liquidation start and last trade.

Any well-typed integrated record that fails one of these cross-field/range checks is
`UNSUPPORTED_CONFIGURATION` from `initialize_engine` before state, bucket, or
counter creation. An intrinsically malformed `DatasetRevision` remains the
integrated Pydantic validation error before an `EngineConfig` exists. After
initialization, replacing any nested revision/config value is `CONFIG_MISMATCH`
against the checkpoint fingerprint. FT-07 neither repairs a revision nor performs
an archive/catalog lookup.

Backtest additionally requires `lane_id=null`; every RunEvent then has
`aggregate_type:"run"` and `aggregate_id=run_id`. Forward paper requires a non-null
`lane_id`; every RunEvent then has `aggregate_type:"lane"` and
`aggregate_id=lane_id`. These values are derived solely from validated config and
are never caller-selected per event. A mismatched mode/lane shape is
`UNSUPPORTED_CONFIGURATION` before state creation.
`force_close` is backtest-only and requires explicit `force_close_at`, the start of
the last selected valid fill bar whose end is at or before `end_at`. `mark_open`
requires null `force_close_at`. A caller cannot change config after initialization;
the canonical config fingerprint in state enforces that boundary.

`max_canonical_bars` is the run-level resource cap. Its default is `500000`, its
valid range is `1..2000000`, and `2000000` is an absolute v1 ceiling that no config
may override. `EngineState.canonical_bar_count` is cumulative across every
`step_engine`, `run_engine`, caller chunk, and checkpoint restore. A valid completed
bar counts one; each missing/invalid quality interval counts its expected open
canonical 60-second slots; closed quality and finish count zero. Semantic replay counts
zero. Before a step, or before any batch item is applied, the engine checks current
state count plus the accepted non-replay delta. Exceeding the configured cap raises
`BATCH_LIMIT_EXCEEDED` before that input/batch creates state, decision, fill, event,
or checkpoint; supplied state remains byte-identical. After the strict ingress
guard, `run_engine` deduplicates only its allowed adjacent semantic replay before
this cumulative preflight, so splitting
the same run into chunks cannot bypass either limit. A configured cap above the
absolute ceiling is `UNSUPPORTED_CONFIGURATION` before state creation.
Pre-start warm-up bars count identically; warm-up is not a cap bypass.

`EmissionContext` is
`{attempt_id:lowercase UUIDv7|null,fencing_token:positive int,
order_submitted_at:UTC timestamp,output_recorded_at:UTC timestamp}`. All values are
explicit pure-call inputs. `attempt_id` and `fencing_token` are opaque output
metadata; the pure engine does not claim, acquire, renew, validate, or persist a
lease. `order_submitted_at` binds any order created by this input and
`output_recorded_at` binds every resulting Fill/RunEvent record. In forward paper,
`event.recorded_at <= order_submitted_at <= output_recorded_at`. Backtest timing is
modeled rather than wall-clock: a completed-bar input requires
`recorded_at=order_submitted_at=output_recorded_at=selection.bar.end_at`; a quality
input requires all three equal `end_at`; and a finish input requires all three equal
`effective_at=config.end_at`. Any other backtest combination is
`VALIDATION_ERROR` before state changes. Inputs that create no order still carry
the fields for one strict event shape, but no order timestamp is emitted. This
closes required record times without an implicit clock or FT-08/FT-10/FT-11
behavior.

`CompletedBarEvent` is exactly:

~~~text
{
  kind:"completed_bar_v1", selection:BarSelection,
  published_base_revision_id:lowercase UUIDv7|null,
  recorded_at:UTC timestamp, emission_context:EmissionContext
}
~~~

`DataQualityEvent` is exactly:

~~~text
{
  kind:"data_quality_v1", start_at:UTC timestamp, end_at:UTC timestamp,
  status:"missing"|"invalid"|"closed",
  reason:"NO_BAR"|"INVALID_BAR"|"MAINTENANCE"|"SCHEDULED_CLOSED",
  source_bar_record_ids:tuple[string,...],
  recorded_at:UTC timestamp, emission_context:EmissionContext
}
~~~

It is a pure, non-strategy control input derived by the caller from canonical
coverage evidence. `recorded_at >= end_at`. The interval is non-empty, aligned to canonical minute
boundaries, covered by the supplied calendar version, does not end after a finite
`config.end_at`, and cannot overlap another
consumed bar or quality interval. `missing` means one or more expected minutes in
a stored open segment had no selected row, uses `reason:"NO_BAR"`, and has no
source IDs. `invalid` means selected raw rows existed but none was eligible, uses
`reason:"INVALID_BAR"`, and carries their lexical unique record IDs. `closed` is
wholly inside exactly one stored non-open segment, has no source IDs, and uses
`MAINTENANCE` or `SCHEDULED_CLOSED` matching that segment. Other
status/reason/source combinations are `VALIDATION_ERROR` with no state change.

`EngineInputEvent` is the strict tagged union
`CompletedBarEvent|DataQualityEvent|FinishRunEvent`; no untyped control mapping is
accepted. Quality intervals ending at or before `start_at` are allowed to describe
warm-up continuity but cannot produce strategy or accounting output.

The integrated `CorrectionObservation` is deliberately not an FT-07 input. It has
no engine-event discriminator and direct submission fails the strict union as
`VALIDATION_ERROR` with detail `UNSUPPORTED_CORRECTION_OBSERVATION` before semantic
input hashing, cursor/correlation advancement, or any state/output. The same ingress
guard requires every `CompletedBarEvent.selection.correction_observations == ()`
exactly. Any non-empty nested tuple, regardless of its `applied` or `reason` values,
returns the same error at the same pre-hash boundary. FT-09 owns correction
observation and its frozen `CORRECTION_OBSERVED` event. Before an interval is
processed, the caller routes every observation to FT-09, then constructs the one
integrated `BarSelection` whose `bar`, `origin`, and `availability_at` name the
causal corrected choice and whose `correction_observations` is empty; FT-07 consumes
only that immutable selection.
After the interval is committed, FT-09 records the late observation and does not
call FT-07. If a caller instead submits a replacement `CompletedBarEvent` for the
already consumed interval, the normal retained-byte/overlap rule returns
`DUPLICATE_CONFLICT`, with no state change or `CORRECTION_OBSERVED` output. For an
accepted CompletedBarEvent, the semantic projection includes the complete integrated
selection—`bar`, `origin`, `availability_at`, and the explicit empty
`correction_observations` array—plus the outer revision and timing fields. The
nested key is never omitted or ignored. Therefore retries differing only by a
non-empty nested observation tuple are not semantic replays: the non-empty form is
rejected before its input is hashed, while two valid empty forms follow the
ordinary exact-byte replay rule. FT-07 never duplicates FT-09 event ownership.

`CompletedBarEvent.selection.bar` must be a canonical 60-second FT-05
`CompletedBar` for the config owner,
source, price basis, and actual contract. `recorded_at >= selection.availability_at`.
For a pinned backtest, the selection is archive-origin, availability equals bar end,
and `published_base_revision_id` equals the pinned revision. For forward paper,
availability is `max(end_at,completed_at,received_at)` and the base revision may be
null while a newer active row is consumed. Exact `bar_record_id` values, not a
mutable latest label, are evidence.

Only quality `valid` enters indicators, setup evaluation, fills, or marks. A raw
`CompletedBarEvent` whose selected row has quality `invalid`, `missing`, or
`duplicate_conflict` is rejected as `INVALID_BAR_EVENT`; it is never converted to
a quality event internally. A selected bar not on tick, invalid OHLC,
negative/non-integral volume, mismatched interval, or noncausal availability is
also `INVALID_BAR_EVENT` with no state change. Callers may subsequently submit an
independently typed `DataQualityEvent` backed by their coverage evidence; that
event emits `DATA_QUALITY` and advances only the deterministic control clock.
`missing` and `invalid` break affected consecutive warm-up and incomplete
aggregates, expire wall-clock state, stale the last mark, and block new entries.
`closed` advances expiry, cutoff, liquidation, and end boundaries but does not
break completed pre-closure indicator history or make a mark stale. No quality
event evaluates rules, creates a setup/order/fill/mark, or fabricates OHLCV.

A canonical completed bar ending after a finite `config.end_at` is
`EVENT_AFTER_END`, even when its start is before the boundary. Rejection occurs
before cursor, bucket, feature, setup, order, fill, accounting, or sequence state
changes. A bar ending exactly at `end_at` is allowed. Bars ending at or before
`start_at` remain permitted warm-up inputs under the rule below; therefore the
engine does not impose a symmetric pre-start rejection.

`FinishRunEvent` is exactly
`{kind:"finish_run_v1",effective_at:UTC timestamp,recorded_at:UTC timestamp,
emission_context:EmissionContext}`.
After the section 4 replay/conflict/terminal dispatch, normal validation of a Finish
on nonterminal state requires backtest mode, exactly-once processing,
`effective_at=config.end_at`, and
`recorded_at>=effective_at`. Every expected open minute from `start_at` through the
finish boundary must already be accounted for by a valid completed bar or explicit
missing/invalid quality event; otherwise it is `VALIDATION_ERROR` with detail
`UNACCOUNTED_OPEN_INTERVAL` and no state change. It advances expiry, marking, and
the declared end policy but can never create a fill at or after `end_at`. No pause,
resume, scheduler, lease, job, recovery, broker, or production-lane control is an
FT-07 input.

`initialize_engine` sets cash, equity, high-water, and the applicable stored
trading-day baseline to starting cash; zeros fees, P&L, exposure, drawdown,
cumulative canonical-bar counter, and sequences; sets all last-slot indexes null;
sets `force_close_state` to `waiting` only for force-close and `not_applicable`
otherwise; and creates no position, order, setup, or feature value.
Canonical events whose bar end is at or before `config.start_at` are warm-up only:
they update buckets, indicators, pivots, zones, and session state but emit no entry,
exit, order, Fill, cash change, exposure, or trading decision. The first entry
evaluation is the first full execution boundary strictly after `start_at`. This is
the same rule for historical and forward modes and prevents warm-up replay from
creating stale orders.

## 4. Event order, causality, aggregation, and deterministic identity

Every `step_engine`/batch item uses this exact preflight order:

1. Validate the config/state fingerprint and strict outer input shape, including the
   pre-hash empty-correction guard in section 3. Failure returns its typed error with
   no semantic hash or state/output.
2. Derive logical identity and `semantic_input_bytes`. If it is the immediately
   preceding semantic replay, return unchanged state and empty deltas first.
3. If the retained semantic bytes changed for the same identity or overlap the
   consumed cursor, return `DUPLICATE_CONFLICT` second.
4. If state is `FINISHED`, `STOPPED`, `BLOCKED_UNCLOSED`, or
   `BLOCKED_EXPIRY_UNRESOLVED`, return `RUN_FINISHED` third for every remaining
   distinct input.
5. Only for nonterminal state apply recorded-time/cursor/resource checks and the
   event-kind-specific validation below, including Finish mode, exact-once,
   `effective_at`, coverage, and unaccounted-open-interval checks.

Steps 2-4 therefore precede all normal `FinishRunEvent` validation. An exact retry
of the Finish that created `FINISHED` replays; a changed same-identity Finish is a
duplicate conflict; every remaining distinct Finish after `FINISHED` or after an
actual-contract `STOPPED`/blocked terminal returns `RUN_FINISHED`, even if its mode,
effective time, recorded time, or coverage would have failed normal Finish
validation. None of these classifications advances a counter or emits another
event. Structurally malformed input and forbidden nested corrections remain step-1
errors because no canonical semantic identity exists for them.

Events are processed in increasing logical interval start and nondecreasing
recorded time. A completed bar's logical interval is its bar interval; a quality
event's is its declared interval; finish sorts after all intervals ending at its
effective time. The state stores the last logical interval, bar record ID when
applicable, payload hash, availability/control time, and input hash. A semantic
replay of the immediately preceding input returns the unchanged state,
empty decision/intent/protective/fill/evidence/event deltas, and `replayed:true`.
A different semantic input overlapping a consumed logical interval is
`DUPLICATE_CONFLICT`; any older semantically nonidentical input is
`EVENT_OUT_OF_ORDER` while state is nonterminal. Terminal precedence is closed in
section 8.
After the step-1 ingress guard, `run_engine` removes its allowed adjacent semantic
replay before the cumulative resource preflight. Incremental chunks may overlap by
their last event, so batch and
incremental results are identical across a caller-boundary duplicate. Arbitrary
old-event lookup and durable deduplication belong to later persistence, not this
bounded checkpoint.

`last_recorded_at` is the accepted input's `output_recorded_at`. Every subsequent
non-replay input requires `event.recorded_at >= last_recorded_at`; its explicit
submission/output times then satisfy section 3. A regression is
`EVENT_OUT_OF_ORDER` with no state or sequence change.

The engine identifies every expected canonical minute from materialized open
calendar segments but never guesses why a minute is absent. Open gaps are advanced
only by an explicit `DataQualityEvent`; a later bar jumping over an unaccounted open
minute is `VALIDATION_ERROR` with detail `UNACCOUNTED_OPEN_INTERVAL` and no state
change. `missing` and `invalid` quality
break every affected consecutive warm-up and incomplete aggregate, expire orders
by wall-clock boundary, and make the last mark stale. Maintenance and
`scheduled_closed` quality events emit no invented bars and do not themselves break
a completed pre-break history. No bar, quality event, or aggregate may cross a
calendar segment.

The engine derives fill, feature, zone, and execution buckets from canonical minute
bars. Buckets anchor at each stored open-segment start. A bucket is usable only when
every expected minute is present and valid. Scheduled short final buckets are
rejected for strategy and fill use even though FT-05 can expose them under an
explicit research partial policy. For target intervals supported by
`aggregate_completed_bars`, derived OHLCV, source ID order, gaps, and boundaries must
match that integrated function exactly. Other valid minute-multiple fill intervals
use the same first/max/min/last/sum and segment-anchor rule.

For any derived fill bar, use the integrated
`AggregatedFullBar.source_bar_record_ids` tuple in its existing timestamp order. The
singular frozen `Fill.bar_record_id` is an availability anchor, never a claimed
trigger component. Select the source `BarSelection` having maximum
`availability_at`; if tied, select the greatest position in the integrated source
tuple. Use that selection's exact bar record ID for every opening-gap, touch, and
both-hit Fill from the aggregate. `FillEvidence` is emitted one-for-one in Fill
tuple order, repeats that anchor with its explicit role, and carries the complete
integrated ordered source-ID tuple, its exact parallel dataset-provenance tuple,
and aggregate bounds. Its `fill_id` must match
the paired frozen Fill. This companion is engine evidence, not an added Fill or
RunEvent-payload field. `fill_evidence` has exactly the same length and order as
`fills`; no Fill can be emitted without its companion.

`MARK_RECORDED.bar_record_id`, `EngineState.last_mark_bar_record_id`, and
`MarkEvidence.close_bar_record_id` are the last ordered source ID because that
actual canonical component supplies the aggregate close. `MarkEvidence.run_event_id`
matches the corresponding `MARK_RECORDED` event. `MarkEvidence` carries the same
complete source tuple, parallel dataset-provenance tuple, and bounds; state retains
them as `last_mark_source_bar_record_ids` and
`last_mark_source_dataset_provenance`. A 60-second aggregate has a one-ID tuple. No
synthetic aggregate ID is substituted and source order is never lexicalized. Thus
the singular frozen fields have explicit non-trigger roles while the companion
evidence makes every derived Fill/mark reconstructable without schema mutation.
`mark_evidence` has exactly one item per `MARK_RECORDED` event, in event order.

For both companions, provenance element `i` repeats source ID `i` and the revision
captured when that source selection entered its fill bucket. Length mismatch,
reordering, duplicate source IDs, an anchor absent from the paired tuple, or a
different revision for the same source is invalid before output emission. The Fill
availability anchor's provenance is the matching tuple element; the mark close
source's provenance is the final element. Companion provenance is part of canonical
step/run output bytes and replay equality. Past FillEvidence need not be copied into
EngineState because no future transition cites a past fill bar; the current mark's
parallel pair is checkpointed because later exit/risk evidence may cite it.

A derived bar's causal availability is the maximum availability of all its source
minutes. At execution-bar end `T`, zone bars newly complete at or before `T` are
processed oldest first, then features and setups are evaluated once for that full
execution bar. Let execution anchor `A` be that aggregate's maximum source
availability. Backtest `effective_at=decided_at=created_at=T`, because the
boundary-completing backtest input is recorded at `T`. Forward `effective_at` is the
maximum of `A` and every source/feature/setup `known_at` used by the evaluation;
`decided_at` is the input `recorded_at` and is never earlier.
`Decision.created_at=decided_at`.
`OrderIntent.submitted_at=EmissionContext.order_submitted_at` and must be at or after
its Decision's `decided_at`. In both modes its `active_from` is the first full
configured fill-bar start at or after both `effective_at` and `submitted_at`. For
entries, that start must be
inside the effective entry window; the entry-window ordering in section 3 then
governs a window close inside the active fill bar. `Fill.created_at` is
`EmissionContext.output_recorded_at` on the input
that determines the fill, while `Fill.model_time` remains the frozen fill-bar start
for an opening gap and end for an intrabar touch. Every RunEvent caused by the input
uses `recorded_at=created_at=output_recorded_at`. Thus the delayed fixture binds
Decision `10:15:02.100000Z`, close submission `10:15:02.200000Z`, activation
`10:16:00Z`, and no fill in the already-open 10:15 bar without an implicit clock.
A close-based signal therefore never fills in its signal bar, and delayed delivery
never reaches back into an already-open bar. The same invariant applies to lifecycle
closes: `submitted_at <= active_from <= Fill.model_time` for every Fill. Backtest
fills and events created while processing a completed fill bar have
`created_at=output_recorded_at` equal the canonical input end that made that fill bar
available; their modeled opening-gap time may be earlier only when the order was
submitted and active no later than that opening instant.

Every trading Decision's `source_bar_record_ids` is the ordered-unique concatenation
of each lexical RuleResult's stored source tuple, the selected setup's stored source
tuple when present, and the current execution aggregate's integrated source tuple,
keeping first occurrence. The aggregate suffix is mandatory, so an evaluated
Decision is never source-free even if every rule node is constant. This source order
applies to HOLD, ARM, ENTRY, CLOSE, REJECT, and the CANCEL projection refined in
section 9.

Decision dataset provenance is computed from the complete ordered cited-source
tuple, not from the current base pointer. Resolve every Decision
`source_bar_record_id` to the matching persisted `SourceDatasetProvenance` companion
in the current bucket, feature/accumulator/history, zone, setup, or last-mark
evidence. Missing or inconsistent lookup is an internal invariant failure and a
restored state containing it is `CHECKPOINT_MISMATCH`; no Decision may be emitted.
For backtest, every resolved element must equal the pinned dataset revision and
`Decision.dataset_revision_id` is that revision. For forward paper it is the one
shared non-null revision only when every resolved element has that exact value; it
is null if any element is null or if two non-null revisions differ. An evaluated
Decision with an empty cited tuple is invalid. The complete ordered source bar IDs
remain authoritative, and publishing a base never patches prior provenance or
Decision bytes.

The checkpoint/base-transition oracle is explicit. Consume source `a` under
published base `R1`, checkpoint/restore, then consume source `b` under later base
`R2` and source `c` as active/null. A Decision citing only `a` projects `R1`; only
`b` projects `R2`; `a,b`, `a,c`, or `b,c` projects null. The restored provenance for
`a` remains `R1` byte-for-byte. Tuple order is the Decision's existing ordered
source-ID order and does not affect this equality reduction.

Record IDs are deterministic lowercase UUIDv7 values without randomness. Define
`deterministic_uuid7(domain,timestamp,fields)` as follows: canonicalize
`["paper-engine-v1",domain,*fields]` under frozen section 13.1; take SHA-256; create
sixteen bytes from the 48-bit floor Unix milliseconds of `timestamp` followed by
the first ten digest bytes; overwrite byte 6's high nibble with `0111` and byte 8's
high two bits with `10`; format as lowercase UUID text. Decimal fields below use
their canonical string, null is JSON null, and no tuple is sorted after assembly.

The domain, UUID timestamp, and ordered `fields` tuple are closed:

| Record | Domain | UUID timestamp | Ordered fields |
| --- | --- | --- | --- |
| Input correlation | `correlation` | modeled input effective time | `run_id,lane_id,correlation_sequence,input_kind,logical_start,logical_end,input_sha256` |
| Decision | `decision` | `effective_at` | `run_id,lane_id,decision_sequence,strategy_version_id,execution_bar_end,decision_type,side,setup_id,pre_state_sha256,correlation_id` |
| Decision idempotency | `decision-idempotency` | `effective_at` | the Decision fields tuple followed by `decision_id` |
| Entry order | `entry-order` | `OrderIntent.effective_at` | `run_id,lane_id,order_sequence,decision_id,setup_id,side,order_type,quantity` |
| Protective stop | `protective-stop-order` | entry `Fill.model_time` | `run_id,lane_id,order_sequence,entry_order_id,entry_fill_id,side,quantity,trigger_price` |
| Protective target | `protective-target-order` | entry `Fill.model_time` | `run_id,lane_id,order_sequence,entry_order_id,entry_fill_id,side,quantity,trigger_price` |
| Market close order | `close-order` | `OrderIntent.effective_at` | `run_id,lane_id,order_sequence,decision_id|null,entry_fill_id,side,quantity,reason` |
| Fill | `fill` | `model_time` | `run_id,lane_id,fill_sequence,order_id,bar_record_id,effect,reason,pre_state_sha256` |
| Zone | `zone` | `known_at` | `run_id,lane_id,creation_sequence,contract_id,zone_interval_seconds,kind,pivot_bar_end,confirmation_bar_end` |
| Setup | `setup` | `known_at` | `run_id,lane_id,setup_sequence,family,side,source_zone_ids,arm_execution_index,signal_execution_index` |
| Run event | `run-event` | `effective_at` | `run_id,lane_id,aggregate_type,aggregate_id,event_sequence,event_type,payload_sha256,causation_id,correlation_id,state_sha256` |

For a completed bar, modeled input effective time is bar end in backtest and
selection availability in forward paper; logical start/end are the bar bounds. For
a quality event they are its start/end and modeled time is `end_at`; for finish
they both equal `effective_at`. `causation_event_id` on a Decision is its input
correlation ID. `correlation_sequence` increments once per accepted non-replay
input; all outputs from that input share its correlation ID. Order sequence is
allocated once for an entry at its Decision; its Fill allocates stop, then target,
then a scheduled force close when required. Any independently created close
allocates once at creation. All next-sequence
counters are stored in `EngineState`, increment only on committed creation, and are
covered by checkpoint/state hashes. Rejection and semantic replay consume no counter.
`source_zone_ids` in setup identity is the snapshot tuple ordered by source-zone
creation sequence then lexical zone ID; it is never iteration order. Null optional
identity fields remain explicit null tuple members.
`payload_sha256` is the section 13.1 SHA-256 of the complete strict RunEvent payload;
it commits `FILL_RECORDED.evidence_mode` and every other payload byte without adding
a public RunEvent field.
Define `semantic_input_bytes` as section 13.1 canonical bytes of the complete typed
input after removing exactly `emission_context.attempt_id` and `.fencing_token`
from the hash projection; their keys are omitted, not replaced by null. Every other
field remains, including event `recorded_at`, `order_submitted_at`,
`output_recorded_at`, the complete valid BarSelection with its explicit empty
`correction_observations` array, selected record/payload/revision, quality sources,
and Finish time. `input_sha256=SHA256(semantic_input_bytes)`, and
`last_input_sha256` stores that value. A forbidden non-empty nested observation is
rejected before this projection and has no `input_sha256`.

Two inputs are byte-identical for replay exactly when their
`semantic_input_bytes` are equal and their logical interval/kind matches. Therefore
a retry differing only in attempt ID and/or fencing token replays the already
committed domain result: it consumes no counter, recomputes/emits no record, returns
empty deltas, and leaves the original Fill/RunEvent transport fields and all IDs
unchanged. The replacement attempt/fence values are not copied into old outputs.
Any difference in any retained field is not replay: the same/overlapping logical
interval is `DUPLICATE_CONFLICT`; while nonterminal, an older non-overlapping interval
is `EVENT_OUT_OF_ORDER`; after terminal, section 8 instead returns `RUN_FINISHED` for
every distinct non-overlapping input. All errors leave state byte-identical. On a new
non-replay input, the supplied attempt/fence remain opaque output metadata and do
not enter any domain UUID tuple. This is an identity exclusion only, not a
pure-engine lease/fence validation claim.

These canonical vectors are normative:

| Domain/timestamp | Fields | UUID |
| --- | --- | --- |
| `correlation` / `2026-01-02T03:05:00Z` | `["018f4c00-0000-7000-8000-000000000001",null,0,"completed_bar_v1","2026-01-02T03:04:00Z","2026-01-02T03:05:00Z","0000000000000000000000000000000000000000000000000000000000000000"]` | `019b7caa-6360-7464-861e-19052358e1e7` |
| `decision` / `2026-01-02T03:05:00Z` | `["018f4c00-0000-7000-8000-000000000001",null,0,"018f4c00-0000-7000-8000-000000000002","2026-01-02T03:05:00Z","ENTRY","long","018f4c00-0000-7000-8000-000000000003","0000000000000000000000000000000000000000000000000000000000000000","019b7caa-6360-7464-861e-19052358e1e7"]` | `019b7caa-6360-740c-8624-091a15fdd2b9` |
| `protective-stop-order` / `2026-01-02T03:06:00Z` | `["018f4c00-0000-7000-8000-000000000001",null,1,"018f4c00-0000-7000-8000-000000000010","018f4c00-0000-7000-8000-000000000011","sell",1,"1999.8"]` | `019b7cab-4dc0-7d0e-b4c8-ad805426627d` |
| `run-event` / `2026-01-02T03:06:00Z` | `["018f4c00-0000-7000-8000-000000000001",null,"run","018f4c00-0000-7000-8000-000000000001",0,"FILL_RECORDED","8d2c44a2b0bf6a6f589a16d67ae99a43ecd45197ef6d5b66fb228bb283334c73","018f4c00-0000-7000-8000-000000000011","019b7caa-6360-7464-861e-19052358e1e7","0000000000000000000000000000000000000000000000000000000000000000"]` | `019b7cab-4dc0-758a-b2fb-4df46dcfbc51` |

The RunEvent vector's payload canonical bytes are
`{"evidence_mode":"historical","fill_id":"018f4c00-0000-7000-8000-000000000011","fill_sequence":0,"kind":"FILL_RECORDED","order_id":"018f4c00-0000-7000-8000-000000000010"}`.
Their SHA-256 is the payload-hash field shown in the vector. The table and vectors,
not host UUID or clock APIs, are the cross-platform oracle.

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

Rule evaluation is always anchored to one completed execution aggregate. With `A`
defined in section 4, each `RuleResult.known_at` is the maximum of `A` and the
`known_at` values of the feature/child evidence used by that node. A literal or a
constant-only subtree has `source_bar_record_ids:()` exactly and `known_at=A`; it
does not invent a source ID or use `recorded_at` as a substitute. Its containing
Decision nevertheless cites the mandatory current aggregate suffix, so in forward
paper both constant-only ENTRY and constant-only CLOSE have
`effective_at=A`, carry the aggregate's exact source IDs/provenance, and cannot be
decided or submitted before `A`. In backtest the same cases retain
`effective_at=execution_bar_end`. This anchor also applies to constant-only FAIL or
UNKNOWN HOLD/REJECT evidence.

The runtime UNKNOWN reason inventory is exactly:

- `NOT_READY_WARMUP`, `NOT_READY_RSI`, `DATA_GAP`, `ZERO_DENOMINATOR`,
  `NUMERIC_DOMAIN`.
- `NOT_READY_SESSION`, `NOT_READY_PRIOR_SESSION`, `NOT_READY_PIVOTS`,
  `NOT_READY_LEVEL`, `NOT_READY_ATR`, `NOT_READY_PEAK_WINDOW`.
- `RULE_CHILD_UNKNOWN` and `FILTER_UNKNOWN`.

Unknown code paths outside this list are implementation defects and cannot become
public reason strings ad hoc.

`NOT_READY_RSI` is mandatory, rather than the generic warm-up code, whenever a
requested Wilder RSI has not yet accumulated its exact seed/delta history or that
history was broken by missing/invalid quality. This is the exact reason projected
by `exit_rule_unknown_does_not_close`; a supplementary exit at that UNKNOWN result
does not close the position. Its leaf `RuleResult.reason_code` and the resulting
HOLD `Decision.reason_code` are both exactly `NOT_READY_RSI`, not
`NOT_READY_WARMUP`, `EXIT_RULE_FAIL`, or free-form prose.

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

Every close `OrderIntent`, regardless of cause, is exactly
`kind:"close",order_type:"market",effect:"close"`, has the opposite side and full
quantity of the current position, has all six raw/executable entry/stop/target price
fields null, has `bracket_template:null`, and copies
`fill_model.entry_bar_exit_policy` and `.both_hit_policy` as required serialized
policy fields even though neither changes market-close fill semantics. No close
intent is created while flat.

The three close causes bind timing and expiry as follows:

| Cause | Creation and activation | `expires_at` |
| --- | --- | --- |
| Passed supplementary exit rule | Created by its `CLOSE` Decision; `effective_at` is Decision effective time and `submitted_at` is the input's explicit order-submission time. In both modes, `active_from` is the first not-yet-processed stored-calendar open-segment-anchored fill-bar start at or after both values. | `contract.liquidation_start_at` (exclusive). |
| Contract liquidation | Created as the persisted `close_intent` when accepted input chronology first reaches `contract.liquidation_start_at`, preserving the frozen create-at-liquidation rule. It has `effective_at=contract.liquidation_start_at`, `submitted_at` from that boundary-advancing input's explicit emission context, and in both modes `active_from` is the first not-yet-processed eligible fill-bar start at or after both values. | `contract.last_trade_at` (exclusive). |
| Backtest force close | `scheduled_force_close` uses the section 8 pre-scheduling rule and moves to `close_intent` at `config.force_close_at`. | `config.end_at` (exclusive). |

At those creation transitions the exit-rule row stores its creating `CLOSE`
Decision ID, while the liquidation and force-close rows store null. Moving a
scheduled force close into `close_intent` preserves that null byte-for-byte.

At most one live `close_intent` exists. Creation appends its exact `OrderIntent` to
`intents` once. `OrderState` is `PENDING` when `active_from` is later and becomes
`ACTIVE` before fill evaluation when that boundary is reached; if already effective,
it is created directly `ACTIVE`. Its initial state and every later
activation, cancellation, or expiry emit the corresponding exact `ORDER_STATE`
transition under section 9. An exit-rule
close persists unchanged
across missing data, maintenance, scheduled closure, delayed delivery, and entry
window closure. At liquidation the pre-existing exit close expires before it is
replaced by the newly created liquidation close. An active liquidation
or force close likewise persists
across closed/data-delay intervals until its exclusive expiry. Protection remains
active while any market close is pending and retains opening-gap precedence.
Whichever close/protective Fill first flattens the position cancels every other
active or scheduled close with `POSITION_FLAT`; cancellation emits no Fill and
retains null `fill_sequence`. Repeated exit PASS while a close is already active
emits one `HOLD` Decision with `CLOSE_PENDING` and no duplicate order. At
exclusive expiry, an unfilled exit-rule order is replaced by the newly created
liquidation order; unfilled liquidation becomes `BLOCKED_EXPIRY_UNRESOLVED`; and
unfilled force close becomes `BLOCKED_UNCLOSED` only on Finish as section 8 states.
Every exclusive expiry first moves the unfilled order to `EXPIRED` with
`ORDER_EXPIRED`, emits that transition, then clears the live pointer; the terminal
state retains the open position and the emitted intent/event evidence. A
`POSITION_FLAT` cancellation similarly emits `CANCELLED` before clearing the live
or scheduled pointer. No cancellation or expiry emits a Fill or commission.

Fill causation is copied from live internal order state before that state is changed
or removed. An entry Fill copies `pending_entry.originating_decision_id`; either
protective Fill copies the selected protective leg's inherited
`originating_decision_id`; and an exit-rule market close copies
`close_intent.originating_decision_id`. Each is therefore the exact originating
`ENTRY` or `CLOSE` Decision even when one or more checkpoints separate Decision and
Fill. Contract-liquidation and force-close lifecycle orders have null
`originating_decision_id`, so their Fills copy null because no trading Decision is
fabricated for either policy action. A non-null causal value is never reconstructed
from `order_id`, `entry_order_id`, `source_setup_id`, correlation, or event history.

For liquidation, "chronology first reaches" means the transition is modeled at the
liquidation boundary, but its order is submitted only at the explicit submission
time of the accepted input that advances chronology there. It can participate in a
bar's opening-gap precedence only if a previously accepted boundary input created
it with `submitted_at <= active_from <= that bar.start_at`. A completed bar whose
start itself first exposes the boundary is not such an input: it is recorded at its
end in backtest (or no earlier than availability in forward), so the newly created
close cannot fill at that already-past open and waits for the next eligible fill-bar
start. The same is true when liquidation falls strictly inside a fill bar. A closed,
missing, or delayed interval can create and persist the intent but cannot fill it.

Entry TTL is an exact half-open count of execution-grid periods. Let `E` be the
creating Decision's `execution_bar_end`, `S=OrderIntent.submitted_at`,
`I=strategy_version.execution_interval_seconds`, and
`N=order_policy.entry_ttl_execution_bars`. Validation guarantees `S>=E`. Define
`k=floor((S-E)/I)` using UTC elapsed seconds, `ttl_start=E+k*I`, and
`expires_at=ttl_start+N*I`. Thus a submission exactly on a boundary starts that
period; a delayed submission inside a period receives only the remainder of that
first period, followed by `N-1` whole periods. The accepted fixture has
`E=S=2026-09-14T10:00:00Z`, `I=900`, `N=2`, and therefore
`expires_at=2026-09-14T10:30:00Z` exactly.

The grid continues in UTC from `E`; missing/invalid bars, maintenance, scheduled
closure, session breaks, and delayed delivery never shift `ttl_start` or extend
expiry. In both modes, `active_from` remains the first not-yet-processed configured
fill-bar start at or after both `E` and `S`, subject to stored-calendar/open and
entry-window eligibility. A fill is eligible only at a modeled instant in
`[active_from,expires_at)`. If expiry lies strictly inside an active fill bar, an
opening-gap fill before expiry remains valid, then the order expires at the exact
boundary before any unknowable intrabar touch. Expiry at bar start precedes its
gap; expiry at bar end precedes a touch modeled at that end. If no eligible fill-bar
start exists before expiry, the order remains PENDING and expires without Fill.

For the delayed-forward boundary vector `E=2026-09-14T10:15:00Z`,
`S=2026-09-14T10:15:02.200000Z`, `I=900`, and `N=1`, expiry is exactly
`2026-09-14T10:30:00Z`; a 60-second fill bar may first activate at `10:16:00Z`, a
gap there is eligible, and a touch modeled at `10:30:00Z` is not. Backtest uses
`S=E`, so the same formula reduces to `E+N*I`.

Effective entry-window close, an FT-07 risk latch, entry cutoff, liquidation, or
run force-close cancels pending entries but never protective exits. No pause,
resume, close-and-stop, operator-risk-control, or inherited control flag exists in
`EngineConfig`; those FT-11 controls remain excluded.

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

Signal-time market sizing has one deterministic oracle and does not reject either
accepted absolute or relative setup-backed configuration. Let `C` be the current
completed execution aggregate close used by the ENTRY Decision. Set reference entry
`P_ref=C+market_slippage_ticks*tick_size` for a buy and
`P_ref=C-market_slippage_ticks*tick_size` for a sell. `C` is already on tick and
the integer-tick result is exact. This sizing reference is causal evidence only; it
is not serialized as a market limit price and does not change the frozen null market
`raw_price`/`executable_price` fields.

For `{kind:"setup_price",field:"stop"}`, use the SetupSnapshot's frozen absolute
executable stop unchanged, then apply adverse stop slippage for the modeled stop
Fill: subtract stop-slippage ticks for a long's sell stop and add them for a short's
buy stop. For relative `{kind:"fixed_ticks"}` or `{kind:"atr_multiple"}`, freeze
the distance/ATR FeatureValue at the Decision, resolve the sizing-only raw stop from
`P_ref` in the loss direction, apply the existing role/side tick rounding, then
apply adverse stop slippage. Relative target specs are resolved from `P_ref` only
for the signal-time geometry check. They remain a frozen template: the market
OrderIntent's relative raw/executable bracket fields stay null exactly as section
13.3 requires. Absolute setup stop/target fields remain serialized and immutable.

Use `P_ref`, that sizing stop, and the quantity-level section 8 formula for both
fixed-contract acceptance and stop-fraction `q0`/decrement selection. At the later
eligible fill bar, replace `P_ref` with the actual bar-open base plus adverse market
slippage. Absolute setup prices do not move; relative stop/target prices are
resolved anew from the actual slipped entry and their frozen template. Re-run
geometry and the same quantity-level risk cap for the already serialized all-or-none
quantity. Neither policy increases or reduces it: any actual gap excess is
`RISK_GAP`, zero Fill/commission, as required by
`market_entry_gap_rejected_by_fill_time_risk`; a passing relative template produces
the accepted `generic_market_entry_resolves_relative_bracket` projection.

The long/short reference boundary is symmetric. With `C=2000.0`, tick `0.1`, one
market-slippage tick, one stop-slippage tick, and a five-tick relative stop, long
`P_ref=2000.1`, raw/executable stop `1999.6`, slipped stop `1999.5`; short
`P_ref=1999.9`, raw/executable stop `2000.4`, slipped stop `2000.5`. Both price-loss
distances are `0.6` before multiplier/quantity. The same absolute stop values as a
setup-backed stop use the same signal risk, but unlike the relative template they
remain fixed when the future bar gaps, so the fill-time risk may increase and reject.

## 8. Cash, marks, sizing, risk, sessions, expiry, and end state

`src/familytrade/simulation/risk.py` owns sizing, fees, cash, realized/unrealized
P&L, marks, high-water/drawdown, daily baselines/counters, and risk latches. Futures
entry never subtracts notional. All accounting uses a local Decimal context with
precision 34 and round-half-even; binary float and the process-global context are
forbidden. The USD pipeline is exactly:

1. Resolve and side-round raw prices to the contract tick, then apply integer-tick
   slippage once to obtain the Fill price. Never money-quantize a price.
2. For each Fill, calculate raw commission as
   `commission_per_contract_per_side * quantity`, then quantize it once to `0.01`
   round-half-even. Add that rounded value to fees and deduct it from cash.
3. An entry changes no notional cash and realizes no P&L. A close calculates raw
   P&L as `direction * (exit_fill_price - entry_fill_price) * multiplier *
   quantity`, then quantizes it once to `0.01` round-half-even. Add rounded realized
   P&L and deduct rounded exit commission; quantize resulting cash once to `0.01`.
4. At each valid mark, calculate raw unrealized P&L with the same price formula and
   quantize it once to `0.01`; equity is `cash + rounded_unrealized_pnl`, quantized
   once to `0.01`. High-water, drawdowns, daily baselines/loss, fees, realized P&L,
   and unrealized P&L are stored as USD cents after their transition.
5. For any prospective integer quantity `q`, compute entry and assumed stop-exit
   commissions independently with the actual Fill pipeline:
   `C_entry(q)=money_round(rate*q)` and `C_exit(q)=money_round(rate*q)`. Compute
   `price_loss(q)=abs(prospective_entry-slipped_stop)*multiplier*q` at full Decimal
   precision, then `modeled_total_loss(q)=price_loss(q)+C_entry(q)+C_exit(q)` with no
   further quantization. The same absolute-distance formula applies to long and
   short. The stop-fraction budget is full-precision
   `risk_fraction * min(starting_cash,current_equity)`. Every inclusive cap test
   compares the unrounded budget/cap directly to `modeled_total_loss(q)`; equality
   is allowed and only `>` fails. One-contract rounded commission may not be
   multiplied by `q`, because that differs from an actual quantity-level Fill at
   half-cent boundaries.

The frozen Fill fields use these exact conventions. `slippage` is a nonnegative
price magnitude `abs(fill_price-base_price)`, never a signed cash amount; adverse
direction is expressed by `fill_price` being higher for a buy or lower for a sell.
Target and intrabar-limit fills with no modeled slippage serialize `"0"`.
`position_quantity_after` is signed contracts: positive long, negative short, and
zero flat. On the only v1 opening Fill, `position_average_after=fill_price`; on the
only v1 full closing Fill it is null. Adds and partial reductions are unreachable
because v1 permits one all-or-none entry and every close is for the full position;
no valid v1 Fill can serialize either case.

`Fill.realized_pnl` is the P&L realized by that Fill, not the cumulative state value
and not net of commission. It is `"0"` for an opening Fill and the cent-quantized
frozen direction/price formula for a full close. `Fill.commission` is the separately
cent-quantized per-fill fee. `cash_after` deducts entry commission on open and, on
close, adds that Fill's realized P&L then deducts exit commission. The state's
`realized_pnl` accumulates closing-Fill values only; state `fees` accumulates every
Fill commission. This keeps fee attribution consistent with the frozen metrics,
which allocate fees later but do not fold them into Fill realized P&L.

With starting cash `"25000"`, tick `"0.1"`, multiplier `"10"`, one contract, and
commission `"1.25"`, the canonical long market/stop pair is:

~~~text
buy open:  base_price="2000", fill_price="2000.1", slippage="0.1",
           commission="1.25", realized_pnl="0", cash_after="24998.75",
           position_quantity_after=1, position_average_after="2000.1"
sell close: base_price="1994", fill_price="1993.9", slippage="0.1",
            commission="1.25", realized_pnl="-62", cash_after="24935.5",
            position_quantity_after=0, position_average_after=null
~~~

Under the same inputs the canonical short market/stop pair is:

~~~text
sell open: base_price="2000", fill_price="1999.9", slippage="0.1",
           commission="1.25", realized_pnl="0", cash_after="24998.75",
           position_quantity_after=-1, position_average_after="1999.9"
buy close: base_price="2006", fill_price="2006.1", slippage="0.1",
           commission="1.25", realized_pnl="-62", cash_after="24935.5",
           position_quantity_after=0, position_average_after=null
~~~

All quoted Decimals above are section 13.1 canonical strings; stored USD values
remain exact cents even when canonical serialization removes a trailing zero.

`EngineState.exposure_seconds` is the frozen numerator "position-open eligible-bar
seconds," not wall-clock time between fills. It changes only when one complete valid
configured fill bar is committed. For each such half-open bar `[B0,B1)`, define
`open_at=B0` when a position carried into the bar, otherwise the opening Fill's
`model_time`; define `close_at` as a closing Fill's `model_time` when it flattens in
that bar, otherwise `B1` while the position remains open. Add
`max(0,close_at-open_at)` in whole seconds exactly once after all fills for the bar.
With the frozen model times this gives the closed matrix:

- opening-gap entry at `B0` and no exit: the whole bar;
- intrabar entry at `B1` and no exit: zero for that bar;
- carried position with opening-gap exit at `B0`: zero;
- carried position with intrabar exit at `B1`: the whole bar;
- opening-gap entry followed by a same-bar stop at `B1`: the whole bar;
- intrabar entry and conservative same-bar stop both modeled at `B1`: zero.

A carried position receives the whole duration only for an eligible completed fill
bar. An incomplete aggregate, missing or invalid expected minute, explicit
missing/invalid quality interval, maintenance, scheduled-closed interval, and every
other closed-market span contribute zero even though position state carries. No
source minute inside a broken fill bucket contributes partially. Pre-start warm-up
and time at or after finite `end_at` contribute zero. Semantic replay contributes zero.
An in-progress fill bucket contributes nothing until completion; its checkpointed
components ensure the eventual bar increments once after restore. The counter is
stored and hashed in every Fill/Event snapshot, never recomputed from unavailable
history, and batch, incremental, and checkpoint replay must return the same integer.

Transition timing is exact: an opening-gap Fill/Event at `B0` precedes accrual for
the rest of that bar. If a later intrabar Fill closes at `B1`, add the bar's exposure
inside that closing Fill's atomic transition before its RunEvents. Otherwise add the
bar delta at `B1` after fill processing and before `MARK_RECORDED`. An intrabar entry
at `B1` includes its zero delta in that entry transition. Consequently the next
Fill/Event pre-state always includes all exposure from earlier eligible bars, while
no opening-gap record prematurely claims future seconds from its current bar.

Canonical boundary examples are: commission `1.005` for one contract becomes
`1.00`, commission `1.015` becomes `1.02`; with tick `0.005`, multiplier `1`, and
quantity `1`, raw close P&L `0.005` becomes `0.00` while `0.015` becomes `0.02`.
With entry `2000.000`, slipped stop `1999.995`, multiplier `100`, and commission rate
`1.005`, quantity one has `price_loss=0.500`, each quantity-level commission `1.00`,
and `modeled_total_loss=2.500`. Quantity two has `price_loss=1.000`, each commission
`money_round(2.010)=2.01`, and total `5.020`; with stop-fraction budget and
per-entry cap both `5.01`, it therefore exceeds the effective cap.
Incorrectly multiplying the rounded one-contract fee would produce `5.000` and is
forbidden. The result is identical for a long stop below entry and an equal-distance
short stop above entry. These examples bind the boundary order and do not alter
tick-side rounding. Marks use the latest valid completed fill-interval close
causally known.

The equity/drawdown series includes starting equity and equity after all events plus
each valid fill-bar close mark, net of costs. High-water never resets. Drawdown is
floored at zero and is bar-close, not intrabar excursion. A missing expected open
bar retains the last price with `mark_status:"stale"`, emits its quality label, and
blocks new entries; it neither invents a mark nor suppresses an already-active
protective exit on a later valid bar.

At signal-time sizing, define `effective_sizing_cap=min(stop_fraction_budget,
risk_policy.per_entry_loss_cap)` for stop-fraction policy. First preserve the frozen
floor: `per_contract_reference_loss=price_loss(1)+C_entry(1)+C_exit(1)` and
`q0=min(sizing_policy.max_quantity,
floor(stop_fraction_budget/per_contract_reference_loss))`. Starting at `q0`, choose
the first integer `q >= 1`
whose quantity-level `modeled_total_loss(q) <= effective_sizing_cap`; equivalently,
decrement the capped candidate deterministically until the nonlinear rounded-fee
formula passes. If none passes, emit `REJECT/RISK_SIZE_ZERO` and no order. In the
`rate=1.005,cap=5.01` example, a candidate two reduces to one.

Fixed-contract policy never reduces its configured quantity. It accepts that exact
quantity only when `modeled_total_loss(q) <= risk_policy.per_entry_loss_cap`; an
excess emits `REJECT/RISK_SIZE_ZERO` with no order. Immediately before any already
serialized entry Fill, recompute prices, geometry, commissions, and
`modeled_total_loss` for that immutable `OrderIntent.quantity`. Stop-fraction uses
the same effective sizing cap recomputed from current equity; fixed uses the
per-entry cap. Neither policy resizes an all-or-none submitted order. An excess at
this stage cancels it with `RISK_GAP`, zero Fill, and zero commission. Thus the
quantity-two boundary reduces to one only during stop-fraction sizing, is rejected
without reduction for fixed sizing, and is cancelled rather than resized if it
arises from a later gap. Entry geometry, prospective risk, one-position state, and
the entry-counter increment are one pure state transition. Rejection reason codes
are exactly `RISK_SIZE_ZERO`,
`ENTRY_GEOMETRY_GAP`, `RISK_GAP`, `DAILY_LOSS_LIMIT`, `DAILY_ENTRY_LIMIT`, and
`CUMULATIVE_DRAWDOWN_LIMIT`.

The effective daily-entry cap is
`min(strategy_version.definition.constraints.max_entries_per_trading_day,
config.risk_policy.max_entries_per_trading_day)`. The strategy constraint and the
run/account risk policy are both authoritative ceilings; neither can widen the
other. The atomic entry fill is allowed only while
`filled_entries_in_trading_day < effective_daily_entry_cap`; equality rejects with
`DAILY_ENTRY_LIMIT` before commission, Fill, or counter advancement.

At a stored trading-day boundary, the last marked prior-day equity becomes
`daily_start_equity`; daily loss and entry count reset, and session VWAP resets.
Position basis, cash, high-water, cumulative drawdown, bracket, setup-independent
order state, and open-position lifetime carry. Daily loss and cumulative drawdown
limits latch inclusively at `>=`; daily entries are allowed only while count is
strictly below the effective limit above. Latches cancel entry/setup state but preserve exits. A
later recovery in equity does not clear a same-day daily-loss latch.

Simultaneous latch ordering is canonical. The closed priority is
`DAILY_LOSS_LIMIT` first, then `CUMULATIVE_DRAWDOWN_LIMIT`; `RiskState.latches` is
always the duplicate-free subsequence of that order. At each valid close mark, first
commit and emit `MARK_RECORDED`, then compute
`daily_loss=max(0,daily_start_equity-equity)` and
`cumulative_drawdown=max(0,high_water-equity)` from that same post-mark cents
snapshot. Collect only newly reached inclusive limits in priority order. If both are
new, one atomic risk transition stores both latches and cancels pending entry/setup
state once using the first member of the newly reached tuple as reason
(`DAILY_LOSS_LIMIT` when both are new); no protection or close intent is cancelled.

After that atomic state transition, emit one `RISK_LATCHED` per newly inserted latch
in priority order. The daily payload uses `observed=daily_loss` and
`limit=risk_policy.daily_loss_cap`; the cumulative payload uses
`observed=cumulative_drawdown` and
`limit=risk_policy.cumulative_drawdown_cap`; both use the current stored
`trading_day`. Their event effective time is the determining mark time. Both event
state hashes include both committed latch flags and the completed cancellation; the
first includes the event counter advanced once and the second the counter advanced
twice. Only after both latch events emit any pending-entry `ORDER_STATE ... ->
CANCELLED` record with the selected first-priority reason; setup-only cancellation
has no fabricated event. Already-latched limits emit no second event. A trading-day
reset removes only `DAILY_LOSS_LIMIT`, preserving canonical order. Semantic replay
emits none and leaves both state and event counters byte-identical.

At `entry_cutoff_at`, cancel entries/setups and block new ones. At
`liquidation_start_at`, cancel entry state and first inspect the position. If flat,
create no close intent, emit no close Decision/Fill, never enter `CLOSING`, and
terminate `STOPPED` with `CONTRACT_CLOSED`. A later
FinishRunEvent is a semantically distinct post-terminal input and returns
`RUN_FINISHED`.
If a position is open, create the liquidation `close_intent` under section 7 and set
`CLOSING`. It remains byte-identical across scheduled closure or missing/invalid
data while protection stays active, then fills at the next eligible fill bar under
normal gap/precedence rules; no stale or closed-market fill is invented. After
close, state is `STOPPED` for that actual contract. If no eligible close exists
before exclusive `last_trade_at`, terminal status is
`BLOCKED_EXPIRY_UNRESOLVED` with the position and last mark visible. A config,
checkpoint, or event naming a different contract ID or record version is
`UNSUPPORTED_CONTRACT_CHANGE`; the engine never stitches, rolls, or transfers a
position.

For a backtest validated to reach liquidation, `mark_open` no longer supplies an
early-success escape at `config.end_at`: the lifecycle must already be `STOPPED`
after a close/flat boundary or `BLOCKED_EXPIRY_UNRESOLVED` at exclusive
`last_trade_at`. Such state is terminal without a Finish input, and any later
`FinishRunEvent` follows the post-terminal `RUN_FINISHED` rule. Conversely, a
`mark_open` backtest ending strictly before liquidation uses its exactly-once Finish
to become `FINISHED` and may retain the frozen open/pending snapshot. A force-close
run ending before liquidation follows its separate completed/blocked Finish path.
No Finish input can convert an unresolved actual-contract liquidation close into
`FINISHED` or `mark_open`.

The immutable `contract_expiry_close_and_no_roll` fixture crosses an FT-07/FT-11
wording boundary. Its complete FT-07 projection is nevertheless asserted: cutoff
cancels pending/new entries; liquidation creates a `CONTRACT_LIQUIDATION` close
intent, keeps protection active, sets `CLOSING`, and creates no stale fill; the
first later eligible bar fills at `1997.90` with realized P&L `-21.00` and exit
commission `1.25`; final engine status is `STOPPED`; new-contract position count is
zero; and automatic roll is false. The phrase `operator intent CLOSE_AND_STOP` is
the FT-11 persistence/control context surrounding that pure projection. FT-07's
test loads the whole expected object, explicitly classifies that phrase as
`external_ft11_context`, and asserts that EngineConfig/state/input/output contain no
operator-intent or control field. It is neither silently skipped nor implemented.

An early `mark_open` run validated to end before liquidation finishes with the
actual open position, pending entry/setup,
close intent, bracket, cash, latest mark/status, realized and unrealized P&L
visible. It creates no
synthetic exit. For `force_close`, initialize `force_close_state:"waiting"` and
block any entry whose lifetime could reach `force_close_at`. Atomically with any
earlier entry fill, create `scheduled_force_close` as a frozen market-close
`OrderState` with `effective_at=active_from=force_close_at`, the entry input's
explicit `submitted_at`, status `PENDING`, reason `FORCE_CLOSE`, and no bracket.
It is separate from `close_intent`, so it neither sets `CLOSING` early nor competes
with protection. A prior ordinary/protective close cancels it and returns
`force_close_state` to `waiting`, allowing a later pre-boundary entry to receive its
own schedule. At the force-close
bar start, activate and move it to `close_intent`, cancel entry state, set `CLOSING`
and `force_close_state:"pending"`, then apply normal opening-gap/bracket/market-close
precedence. Because the scheduled order existed before the bar, its opening Fill is
never backdated from an end-of-bar decision. A
successful close or protective exit cancels its sibling/close as applicable, sets
`force_close_state:"completed"`, keeps entry creation blocked, and returns the
nonterminal engine status to `ACTIVE` while awaiting the required finish input. A
flat position at the boundary creates no close intent or Fill, never enters
`CLOSING`, and moves directly to `completed`; only the later Finish input makes the
run `FINISHED`.

Only the exactly-once `FinishRunEvent` creates `RUN_FINISHED` and terminal
`FINISHED` for a successful early `mark_open` or completed force-close run. If the
force
bar was missing/invalid or the position/close remains unresolved, that finish
atomically sets `force_close_state:"blocked"` and `BLOCKED_UNCLOSED`; it never
backdates a fill. Replaying a semantically identical FinishRunEvent, including one
differing only in excluded attempt/fence metadata, returns unchanged
terminal state with `replayed:true` and emits no second terminal event. Post-terminal
classification then follows one exact order for every input kind: first, an
immediately preceding semantic replay after excluding only attempt/fence returns
the replay result; second, a semantically changed retained payload with the same
logical identity or any overlap with the consumed terminal cursor returns
`DUPLICATE_CONFLICT`; third, every other semantically distinct input returns
`RUN_FINISHED`. Thus a new/disjoint later input and a non-overlapping older input are
both `RUN_FINISHED`, never `EVENT_OUT_OF_ORDER`, after terminal status. All three
outcomes leave terminal state and counters byte-identical; only the first has
`replayed:true`. Forward paper cannot force-close. Actual-contract liquidation
remains the separate `STOPPED`/
`BLOCKED_EXPIRY_UNRESOLVED` path above and does not wait for a run finish event.

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

Backtest decisions always name the validated pinned dataset revision. Forward
decisions use the per-cited-source provenance reduction in section 4: one shared
published base or null for mixed/active evidence. Exact source bar IDs remain
authoritative. A later publication or correction never patches prior bytes. A bar
at or before the committed cursor with a different record ID is rejected and cannot
recompute cash, decisions, fills, or indicators.

The closed Decision emission contract is:

- `HOLD`: an actually evaluated entry/exit/filter/setup outcome produces no stateful
  trading action, including FAIL, UNKNOWN, no eligible zone, cooldown, or
  `CLOSE_PENDING`. No full evaluation means no HOLD.
- `ARM`: one setup atomically enters/updates its armed or signalled state without
  emitting an order.
- `ENTRY`: exactly one entry `OrderIntent` is emitted; it is the only Decision type
  permitted to carry `order_intent.kind:"entry"`.
- `CLOSE`: an exit-rule PASS emits one market-close `OrderIntent`; it is the only
  Decision type permitted to carry `order_intent.kind:"close"`. Contract and
  force-close schedules are deterministic lifecycle actions and emit no Decision.
- `CANCEL`: only an execution-bar evaluation may emit this type. In v1 the exact
  case is `SETUP_EXPIRED` at the completed execution-bar boundary; it may also cancel
  that setup's pending entry without a Fill. Mechanical OCO cancellation and
  wall-clock order lifecycle boundaries are `ORDER_STATE`, not trading Decisions.
- `REJECT`: an otherwise actionable candidate is rejected by conflict, target/
  geometry, sizing, or risk policy and emits no order/fill.

At one accepted input, derive atomic candidates from the one prescribed pre-state,
then serialize Decisions by `(stage_rank,side_rank,family_rank,setup_id,reason_code)`.
Stage ranks are execution-bar setup expiry/cancellation `0`, exit evaluation/action
`1`, entry-rule evaluation `2`, setup arm/signal `3`, entry-intent/conflict `4`, and
sizing/risk rejection `5`. Side ranks are long `0`, short `1`, null `2`; family ranks are
breakout `0`, reversal `1`, none `2`; null setup ID sorts after UUIDs. This ordering
does not change the already-derived conflict/winner outcome.

For each serialized Decision, `pre_state_sha256` hashes state immediately before
that Decision, including the current `next_decision_sequence`. Generate
`decision_id` from that sequence/pre-hash/correlation tuple; then derive its
idempotency key and any order ID, apply only that Decision's semantic state changes,
and, for an `ENTRY` or `CLOSE` carrying an intent, set the created `OrderState`'s
`originating_decision_id` to that exact `decision_id`. Then increment
`next_decision_sequence` once and compute `post_state_sha256`. The causal field is
therefore present in the Decision post-state and every checkpoint made after order
creation. A HOLD or REJECT still changes the sequence and therefore has a new post
hash. With multiple
Decisions, each prior post hash is byte-identical to the next pre hash. Any fill,
mark, or other transition prescribed before decision evaluation is already in the
first Decision's pre-state. No fill, mark, or RunEvent-sequence transition occurs
between serialized Decisions: all Decisions for the input are applied in the order
above before their `DECISION_RECORDED` events are appended in that same order and
before any consequent later transition. This removes UUID/hash circularity and
makes batch/checkpoint replay identical.

A `CANCEL/SETUP_EXPIRED` Decision is fully projected from that completed execution
bar: `execution_bar_end` is the bar end; backtest `effective_at` equals it and
forward `effective_at` is the maximum of execution anchor `A` and the snapshot's
stored `known_at` evidence; `side` and
`setup_id` copy the expiring snapshot; `order_intent` is null; evidence uses the
already permitted `evaluation_stage:"arm"`, its exact evidence mode, an empty
`results` tuple, and that snapshot as `selected_setup`. Its
`source_bar_record_ids` is the ordered-unique concatenation of the snapshot's stored
source tuple followed by the current execution aggregate's integrated source tuple,
keeping first occurrence. `decided_at`, causation, sequence, IDs, and hashes use the
normal Decision rules above.

Window close, contract cutoff, risk latch, force-close cutoff, liquidation, and
entry TTL/absolute expiry are wall-clock lifecycle transitions, never `CANCEL`
Decisions—even when a boundary happens to equal an execution-bar end. When an order
exists, its exact `ORDER_STATE` event has `effective_at` equal to the boundary,
`recorded_at` from the boundary-advancing input, the input correlation ID, and no
invented source-ID/evidence fields; its payload remains the frozen five-field
shape. A setup-only cancellation updates the typed `SetupState` and checkpoint with
the same stable reason but emits no fabricated event type. When a boundary lies
inside a bar, its transition precedes the permitted fill processing defined in
sections 3 and 7. Thus no non-bar instant is forced into a Decision whose frozen
backtest invariant requires `effective_at=execution_bar_end`.

The stable domain reason inventory is closed for v1:

- Evaluation/setup: `ENTRY_RULE_PASS`, `ENTRY_RULE_FAIL`, `EXIT_RULE_PASS`,
  `EXIT_RULE_FAIL`, `SETUP_SIGNAL`, `ARM_LONG`, `ARM_SHORT`,
  `ENTRY_INTENT_LONG`, `ENTRY_INTENT_SHORT`, `FILTER_FAIL`, `FILTER_UNKNOWN`,
  `NOT_APPROACHING`, `NOT_RETESTED`, `NO_ELIGIBLE_ZONE`, `COOLDOWN_ACTIVE`,
  `CLOSE_PENDING`,
  `CONFLICT_OPPOSING_ARMS`, `CONFLICT_OPPOSING_SIDES`, `BREAKOUT_WINS`,
  `SETUP_EXPIRED`, `NO_TARGET_ZONE`, `INVALID_TARGET_DISTANCE`, and
  `INVALID_BRACKET`, plus the section 5 UNKNOWN codes.
- Order/cancellation: `ORDER_SUBMITTED`, `ORDER_ACTIVATED`, `ORDER_EXPIRED`,
  `ENTRY_WINDOW_CLOSED`, `ENTRY_CUTOFF`, `CONTRACT_LIQUIDATION`,
  `FORCE_CLOSE`, `POSITION_FLAT`, `RISK_SIZE_ZERO`, `ENTRY_GEOMETRY_GAP`, `RISK_GAP`,
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
  "closed",reason:"NO_BAR"|"INVALID_BAR"|"MAINTENANCE"|
  "SCHEDULED_CLOSED",source_bar_record_ids}`. Its combinations are exactly the
  `DataQualityEvent` combinations in section 3.
- `RUN_FINISHED` with `{kind,status,end_policy,cash,equity,realized_pnl,
  unrealized_pnl,position_open,pending_entry,mark_status,reason|null}`.

`CORRECTION_OBSERVED` is intentionally absent from this FT-07 subset. Its frozen
shape and meaning are unchanged, but FT-09 owns validation, hashing, persistence,
and emission. FT-07 therefore has no correction-event UUID, payload hash, sequence,
or replay case and cannot return that event from either engine result type.

`FILL_RECORDED.payload.evidence_mode` is exactly `"historical"` for every backtest
Fill and `"contemporaneous"` for every forward-paper Fill. A delayed forward bar is
still contemporaneous because the Fill is created only at its causal availability
and never backdated into an already-open bar. `"reconstructed_after_outage"` is not
emitted by this pure FT-07 engine because it has no durable recovery input; later
lane recovery may use that frozen label without changing these two mode bindings.
The derived `aggregate_type`, `aggregate_id`, evidence mode, event payload, and
event-state hash are fixed before the RunEvent UUID is derived, so retry transport
metadata cannot alter any of those semantic bytes or the ID.

The frozen `ORDER_STATE` payload is exactly
`{kind:"ORDER_STATE",order_id,from,to,reason}`. Its closed transition vocabulary is
`from:"NOT_CREATED"|"PENDING"|"ACTIVE"` and
`to:"PENDING"|"ACTIVE"|"FILLED"|"EXPIRED"|"CANCELLED"`. `NOT_CREATED` is the
required non-null payload-only origin sentinel; it is never an `OrderState.status`
and no null or empty string is serialized. A regular entry/close or scheduled force
close whose `active_from` is later is initially `NOT_CREATED -> PENDING` with
`ORDER_SUBMITTED`, then `PENDING -> ACTIVE` with `ORDER_ACTIVATED`. Stop and target legs created active by
an entry Fill each emit `NOT_CREATED -> ACTIVE` with `ORDER_ACTIVATED`, stop first.
An order already active at creation uses that same direct transition rather than a
fictitious PENDING hop. Fill, expiry, and cancellation transitions originate from
the order's actual `PENDING` or `ACTIVE` state and use the stable reason inventory;
`FILLED`, `EXPIRED`, and `CANCELLED` are terminal and never appear as `from`.

Entry and close IDs resolve to `OrderState.intent`. Protective IDs resolve only to
the fixed `ProtectiveBracketState.stop` or `.target` records, whose role, type, side, trigger,
entry causation, and lifecycle fields are serialized in state/checkpoint bytes.
Protective records never appear in the `intents` tuple or in
`Decision.order_intent`; they appear in the dedicated `protective_orders` delta.
This makes bracket persistence unambiguous without changing the frozen payload or
`OrderIntent` schema.

Events are returned in exact processing order. Within each completed fill bar:
apply boundary/submission/activation transitions effective at or before bar start;
apply opening-gap precedence; apply any internal entry-window close/cutoff/expiry;
then apply only the permitted intrabar precedence. After all newly completed fill
bars, apply execution-bar Decisions and then their order transitions, close mark,
risk latch, and state/finish record. A boundary outside a completed bar retains its
exact modeled position among those actions. The
serialized Decision group follows section 9, so its `DECISION_RECORDED` events are
adjacent and ordered exactly like `decisions`. Each event sequence increments once.
`attempt_id` and `fencing_token` are copied unchanged from `EmissionContext` into
each Fill/RunEvent. The correlation ID is a deterministic UUIDv7 in the
`correlation` domain for the input identity. A later issue may validate ownership and persist
these immutable records atomically but may not reinterpret them.

The hash/identity snapshot algorithm is closed. On an accepted non-replay input,
the engine reserves the current correlation sequence, derives the correlation ID,
increments `next_correlation_sequence`, and applies the validated input cursor,
canonical-bar, interval-bucket, and prior ordered transitions before any dependent
Fill/Decision snapshot. All following hashes are section 13.1 hashes of the complete
typed `EngineState`, including every next-sequence counter, scheduled order, and
live `originating_decision_id` field.

For a prospective Fill, let `f=state.next_fill_sequence` and let
`fill_pre_state_sha256` be the state hash after activation and every earlier action/
event for the input, but before any part of this Fill transition. The Fill UUID tuple
in section 4 uses exactly that internal hash and `f`. After deriving `fill_id`, apply
one atomic Fill transition: first copy the selected order's checkpointed
`originating_decision_id` into `Fill.causation_decision_id`; set the filled order's
`fill_sequence=f`; update its
status, cash, fees, per-fill/cumulative P&L, signed position, risk counters, and
bracket/OCO state. For an entry Fill, copy the same non-null ID into both protective
records before the pending-entry pointer is cleared; allocate entry-created order IDs
stop, target, then scheduled force close when applicable, with the scheduled
force-close causal field null; increment `next_order_sequence` for each allocation;
and finally set `next_fill_sequence=f+1`. For an exit or lifecycle close, capture the
close field before clearing that pointer. The Fill's `*_after` fields project this
post-transition state. No Fill event is emitted until the whole transition is
complete, so no hashed state exposes an unprotected newly opened position.

The ordered UUID field tuples in section 4 do not gain another member. Entry and
exit-rule order identity already includes the creating `decision_id`; lifecycle
close identity already includes explicit null. The standalone normative UUID rows
remain exact. State hashes and any Fill/RunEvent UUID whose supplied pre-state hash
contains a live order are nevertheless recomputed from the enriched canonical state;
the causal field is committed through that hash rather than appended twice. In
particular, a same-bar protective Fill hashes the two inherited IDs in the complete
post-entry bracket state.

Each RunEvent then uses a separate deterministic reservation. Let
`e=state.next_event_sequence` after its domain transition and all earlier emitted
events. Set `next_event_sequence=e+1`, hash that post-domain/post-reservation state
as `event_state_sha256`, derive `run_event_id` from the section 4 tuple using
`sequence=e` and that hash, set `RunEvent.state_sha256=event_state_sha256`, and append
the event without another state mutation. This is non-circular because RunEvent IDs
are not stored in `EngineState`. Multiple events for one atomic Fill share its fully
committed domain state but have distinct hashes because each includes its own
advanced event counter.

RunEvent causation and modeled time are exact: `DECISION_RECORDED` uses the Decision
ID/time; `ORDER_STATE` uses the order ID and status-transition boundary;
`FILL_RECORDED` uses the Fill ID and `model_time`; `MARK_RECORDED` uses the close
component bar ID and fill-bar end; `RISK_LATCHED` uses null and the determining mark
time; `DATA_QUALITY` uses null and quality `end_at`; `RUN_FINISHED` uses null and the
Finish effective time. Every event retains its input correlation ID.

For an entry Fill, emit `ORDER_STATE ACTIVE->FILLED`, `FILL_RECORDED`, stop
`NOT_CREATED->ACTIVE`, target `NOT_CREATED->ACTIVE`, then any scheduled force-close
`NOT_CREATED->PENDING`. If conservative same-bar handling then fills the stop, its
`fill_pre_state_sha256` is the complete post-entry state after all those event-counter
increments. Apply the stop Fill atomically, then emit stop `ACTIVE->FILLED`, its
`FILL_RECORDED`, target `ACTIVE->CANCELLED`, and any additional `POSITION_FLAT`
cancellations by ascending `order_sequence`. Thus entry and stop use consecutive
fill sequences, the stop UUID commits to the created bracket and all entry-side
counters, and replay/checkpoint chunking cannot change either identity. A market
close analogously emits its filled transition and Fill event before stop, target,
then other `POSITION_FLAT` cancellations in ascending order sequence.

## 10. Pure state, checkpoint, replay, and failures

`EngineState` contains only deterministic engine state:

- Config fingerprint, status, last consumed logical interval/record/payload/input
  hash, exact source IDs and their parallel immutable dataset provenance needed by
  open aggregates/evidence, and next deterministic decision/order/fill/event/zone/
  setup/correlation sequences.
- In-progress interval buckets; quantized feature recurrence/history sufficient for
  the validated maximum lookback; confirmed pivots/zones; setup state and frozen
  candidate snapshots.
- Pending entry, position, protective bracket, close intent, scheduled force close,
  cash, equity, marks, fees, realized/unrealized P&L, exposure,
  high-water/drawdown, trading-day baseline, counters, and risk latches.
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

Restore additionally enforces the internal causal invariants before returning any
state: every entry `OrderState` has a non-null lowercase UUIDv7
`originating_decision_id`; each protective leg has a non-null lowercase UUIDv7 and
both legs of one bracket equal each other; `scheduled_force_close` has null; and a
close intent has either the non-null originating exit-rule Decision ID or null for
contract liquidation/force close. Unknown keys, a missing field, a malformed ID,
an entry null, unequal protective IDs, or a non-null scheduled-force-close value is
`CHECKPOINT_MISMATCH` with no restored state. The records have the exact strict
shape shown in section 2; their canonical object keys remain lexically sorted under
frozen section 13.1, and the fields are included in `state_sha256`. Restore does not
need or permit an unbounded Decision history or pending-causation map.

Checkpoint serialization and restore are pure payload behavior only. FT-07 creates
no production checkpoint row/file, migration, lease, fence, lock, transaction,
recovery worker, notification, or deployment compatibility promise. FT-11 and
FT-18 own those boundaries.

`EngineErrorCode` is closed to:

- `VALIDATION_ERROR`, `INVALID_BAR_EVENT`, `EVENT_AFTER_END`,
  `BATCH_LIMIT_EXCEEDED`, `UNSUPPORTED_CONFIGURATION`,
  `UNSUPPORTED_FILL_INTERVAL`, `UNSUPPORTED_CONTRACT_CHANGE`.
- `CONFIG_MISMATCH`, `CHECKPOINT_MISMATCH`, `EVENT_OUT_OF_ORDER`,
  `DUPLICATE_CONFLICT`, and `RUN_FINISHED`.

The code, sole public message, and condition are:

| Code | Fixed message | Condition |
| --- | --- | --- |
| VALIDATION_ERROR | Engine input is invalid. | A typed cross-field invariant not assigned a narrower code fails. |
| INVALID_BAR_EVENT | Bar event is invalid. | Bar quality, identity, OHLCV, tick, interval, or causal time is ineligible. |
| EVENT_AFTER_END | Bar event is after the run end. | A completed bar ends after finite `config.end_at`. |
| BATCH_LIMIT_EXCEEDED | Engine batch bar limit is exceeded. | Cumulative accepted canonical bars would exceed `max_canonical_bars`. |
| UNSUPPORTED_CONFIGURATION | Engine configuration is unsupported. | Currency, strategy status/hash/catalogue, owner, dataset identity/coverage, calendar, mode, random seed, five-year span, absolute bar ceiling, fill-interval equality, or policy is unsupported/inconsistent. |
| UNSUPPORTED_FILL_INTERVAL | Fill interval is unsupported. | It is sub-minute, not a whole minute, larger than execution, or does not divide execution. |
| UNSUPPORTED_CONTRACT_CHANGE | Contract change is unsupported. | Contract ID or record version differs from the initialized checkpoint/config. |
| CONFIG_MISMATCH | Engine configuration does not match state. | Any non-contract config fingerprint field differs. |
| CHECKPOINT_MISMATCH | Engine checkpoint is invalid. | Format/engine version, state hash, or typed checkpoint invariant fails. |
| EVENT_OUT_OF_ORDER | Engine event is out of order. | While nonterminal, a distinct non-overlapping input is older than the consumed cursor or recorded time regresses. |
| DUPLICATE_CONFLICT | Logical bar has conflicting content. | After the immediate replay check, a consumed logical identity reappears with different retained semantic input bytes or a changed input overlaps the consumed interval; this specific conflict also precedes the terminal guard. |
| RUN_FINISHED | Engine run is already terminal. | After replay and duplicate/overlap checks, any other semantically distinct input is supplied to a finished/stopped/blocked terminal state. |

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
- `test_incremental_overlap_semantic_replay_returns_empty_deltas_once`
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
- `test_post_end_completed_bar_is_rejected_without_state_or_fill_and_prestart_warmup_is_allowed`
- `test_data_quality_event_missing_invalid_closed_and_raw_invalid_bar_boundaries`
- `test_protective_stop_and_target_records_are_strict_without_mutating_order_intent`
- `test_per_domain_uuid7_tuples_counters_and_normative_vectors_are_exact`
- `test_effective_entry_windows_intersect_run_and_strategy_without_flattening`
- `test_usd_commission_pnl_and_risk_rounding_pipeline_boundary_examples`
- `test_batch_default_and_absolute_bar_limits_fail_before_state_creation`
- `test_uuid_vector_integer_quantity_matches_normative_value`
- `test_cumulative_bar_caps_across_steps_chunks_restore_and_five_calendar_year_span`
- `test_checkpoint_restores_accumulators_and_runtime_indexes_byte_exactly`
- `test_aggregate_fill_uses_availability_anchor_and_complete_ordered_source_evidence`
- `test_aggregate_mark_uses_close_component_and_complete_ordered_source_evidence`
- `test_forward_decided_submitted_active_and_created_times_match_delayed_fixture`
- `test_effective_daily_entry_cap_is_minimum_of_strategy_and_risk_policy`
- `test_window_close_inside_fill_bar_allows_opening_gap_then_cancels_before_touch`
- `test_window_close_at_fill_bar_start_and_end_orders_gap_and_touch_exactly`
- `test_internal_window_open_never_retroactively_activates_an_entry`
- `test_force_close_waits_for_exactly_once_finish_before_finished`
- `test_all_close_intent_shapes_activation_persistence_cancellation_and_expiry_are_exact`
- `test_flat_at_liquidation_stops_without_close_or_closing_and_finish_returns_run_finished`
- `test_flat_at_force_close_waits_for_finish_without_close_or_closing`
- `test_contract_expiry_fixture_projects_all_fields_without_ft11_control_state`
- `test_rule_result_and_decision_evidence_match_frozen_schema_exactly`
- `test_order_fill_sequence_is_null_until_fill_and_advances_only_on_commit`
- `test_decision_type_emission_order_uuid_and_pre_post_hash_chain_are_exact`
- `test_signed_zero_and_negative_protective_triggers_require_finite_tick_geometry`
- `test_setup_expiry_cancel_decision_has_exact_completed_bar_projection`
- `test_policy_window_cutoff_ttl_and_risk_transitions_emit_no_cancel_decision`
- `test_order_state_creation_uses_not_created_for_pending_and_direct_active`
- `test_fill_and_run_event_hash_snapshots_include_all_counters_and_same_bar_stop`
- `test_long_and_short_fill_field_conventions_match_canonical_examples`
- `test_entry_ttl_backtest_and_delayed_forward_half_open_alignment_is_exact`
- `test_exposure_seconds_gap_intrabar_quality_checkpoint_and_replay_matrix`
- `test_run_event_aggregate_identity_payload_hash_and_fill_evidence_mode_bytes`
- `test_attempt_fence_only_semantic_replay_and_retained_field_conflicts`
- `test_backtest_record_decide_submit_output_and_created_times_equal_modeled_boundaries`
- `test_entry_exit_liquidation_and_force_close_opening_gaps_never_precede_submission`
- `test_decision_dataset_revision_is_shared_base_or_null_from_all_cited_sources`
- `test_checkpoint_preserves_bounded_source_provenance_across_base_transition`
- `test_post_terminal_replay_duplicate_conflict_and_run_finished_precedence`
- `test_constant_only_forward_entry_uses_execution_aggregate_causal_anchor`
- `test_constant_only_forward_close_uses_execution_aggregate_causal_anchor`
- `test_fill_interval_must_equal_strategy_version_without_state_creation`
- `test_fill_and_mark_evidence_provenance_pairs_source_ids_exactly`
- `test_run_event_nullable_fencing_token_round_trips_but_engine_emits_positive`
- `test_simultaneous_risk_latches_have_canonical_state_event_hash_and_replay_order`
- `test_backtest_dataset_revision_cross_fields_and_coverage_reject_without_state`
- `test_quantity_level_commission_cap_1_005_qty2_long_short_and_policy_matrix`
- `test_signal_time_market_sizing_absolute_and_relative_stops_long_short`
- `test_market_fill_time_gap_revalidates_without_resizing_either_sizing_policy`
- `test_liquidation_reaching_backtest_requires_coverage_through_last_trade`
- `test_finish_cannot_strand_or_finish_unresolved_actual_contract_liquidation`
- `test_correction_observation_rejected_before_hash_state_and_output`
- `test_pre_cursor_correction_enters_only_as_caller_selected_bar`
- `test_terminal_dispatch_precedes_normal_finish_validation_for_every_terminal_state`
- `test_completed_bar_rejects_nested_corrections_before_hash_cursor_and_state`
- `test_retry_differing_only_by_nested_corrections_is_invalid_not_replay`
- `test_rules_only_entry_fill_after_checkpoint_retains_originating_entry_decision`
- `test_setup_entry_fill_after_checkpoint_retains_originating_entry_decision`
- `test_protective_fill_after_checkpoint_inherits_originating_entry_decision`
- `test_exit_rule_close_fill_after_checkpoint_retains_originating_close_decision`
- `test_lifecycle_close_fill_after_checkpoint_has_null_causation_decision`
- `test_order_causation_fields_are_bounded_strict_and_checkpoint_byte_exact`

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
