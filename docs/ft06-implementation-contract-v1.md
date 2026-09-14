# FT-06 implementation contract v1

Status: CONTRACT_CANDIDATE_FOR_IDENTICAL_BYTE_REVIEW.

This document is an additive implementation binding for FT-06 only. It does not
alter any frozen FT-02 member. It commissions no application implementation by
itself and includes no FT-07 evaluation, fill, lane, provider, deployment, purchase,
or live-order behavior.

## 1. Evidence, compatibility, and precedence

The accepted implementation base is
3c8132f7e7016175853f59d6c6534faec3c63e2b on origin/main. The following commits
are ancestors of that base:

- FT-05 candidate 66e5e30d2483965fd9b6fac0e10ab180d8a5944c.
- FT-04 integrated base 963cf68f3d88333813af56d3483ec9451b74146d.
- FT-02 contract commit 5aa4e7766846e889d45ac87c5da80db796869017.

The frozen FT-02 packet has 37 manifest members and ordered packet SHA-256
9bb0e22bb72ccc0dc1db56ddcb8ba9106b5260f92710b2597072247c3f5f723d.
All 37 member hashes and byte lengths must still match before implementation and
after integration.

Normative precedence is:

1. docs/contracts-v1.md sections 2-3, 5-7, and 13.
2. The FT-06 recipe in docs/issue-recipes-v1.md and docs/issues/FT-06.md.
3. The accepted docs/strategies/configurability-v1.md clarification.
4. This document, only where the sources above deliberately leave an engineering
   binding open.
5. The Reversal-Breakout draft as parameter provenance only.

If this document conflicts with an item above it, the higher-precedence text wins
and implementation returns WAIT_CONTRACT_GAP. The closed type/unit names, raw
parser pipeline, numerical warm-up algorithm, calendar lookup, and same-connection
authentication seam below are intentional v1 implementation bindings in previously
unspecified space. They preserve all accepted fixture wire values and trading
meaning. They do not reinterpret stored FT-02 bytes.

The repository issue state was revalidated on 2026-09-14: FT-06 issue 7 is open;
its direct dependencies FT-02 issue 3 and FT-04 issue 5 are closed. FT-05 PR 22 is
merged at the accepted base. FT-05 issue 6 remains open as a ledger inconsistency,
but it is not a published direct dependency of FT-06 and does not erase the merged
code. There was no FT-06 branch, pull request, or other in-flight owner at contract
authoring time.

## 2. Exact public surface and raw-input boundary

### 2.1 Definition models

src/familytrade/strategies/definitions.py exports these strict, frozen Pydantic v2
models:

- RuleDefinition, FeatureInstance, EntryRules, ExitRules, OrderPolicy,
  StrategyConstraints, and EntryWindow.
- FeatureNode, ConstantNode, ArithmeticNode, CompareNode, TemporalCompareNode,
  and GroupNode, united as RuleNode.
- FeaturePriceSource, SetupPriceSource, SetupStop, FixedTicksStop,
  AtrMultipleStop, SetupTarget, FixedTicksTarget, and RiskMultipleTarget,
  with PriceSource, StopSpec, and TargetSpec unions.
- OnePositionSetup, ConfirmedPivotZonesSetup, ReversalSetup, and
  BreakoutRetestSetup, united as SetupModule.
- StrategyDraftFromDefinitionInput, StrategyDraftFromVersionInput,
  StrategyDraftCreateInput, StrategyDraftEditInput,
  StrategyDraftValidateInput, StrategyListInput, StrategyVersion,
  StrategyListItem, StrategyPage, ValidationIssue,
  DefinitionValidationResult, and StrategyValidationResult.

Every model uses extra="forbid", strict types, and hidden input values in
validation errors. Tagged unions use kind as their discriminator. A bool is never
an integer. Public decimal, price, volume, rate, money, and multiplier values are
canonical decimal strings, never JSON floats.

The operation input shapes are exactly:

~~~text
StrategyDraftFromDefinitionInput = {
  kind:"definition", schema_version:"v1", name:string[1..120],
  definition_schema_version:"rule-strategy-v1",
  definition:RuleDefinition, catalogue_version:"feature-catalogue-v1",
  execution_interval_seconds:300|900|1800|3600,
  fill_interval_seconds:positive int default 60
}

StrategyDraftFromVersionInput = {
  kind:"source_version", schema_version:"v1", name:string[1..120],
  source_version_id:lowercase UUIDv7
}

StrategyDraftEditInput = {
  schema_version:"v1", draft_id:lowercase UUIDv7, expected_version:int>=1,
  name:string[1..120], definition_schema_version:"rule-strategy-v1",
  definition:RuleDefinition, catalogue_version:"feature-catalogue-v1",
  execution_interval_seconds:300|900|1800|3600,
  fill_interval_seconds:positive int
}

StrategyDraftValidateInput = {
  schema_version:"v1", draft_id:lowercase UUIDv7, expected_version:int>=1
}

StrategyListInput = {
  schema_version:"v1", status:null|"draft"|"validated",
  cursor:null|string, limit:int[1..200] default 50
}
~~~

Exactly one create variant is accepted. A source-version create may clone only an
owned validated version. It copies definition schema, catalogue, intervals, and
definition; replaces both persisted name and definition.name with the requested
NFC-normalized name; and derives a new hash and UUIDv7. Direct create and edit
require name to equal definition.name after NFC normalization. Mismatch is
NAME_MISMATCH.

fill_interval_seconds must be no larger than, and divide, the execution interval.
Every feature interval is one of 60, 300, 900, 1800, or 3600 seconds, is no larger
than the version execution interval, and divides it. The existing zone/execution
catalogue restrictions still apply to the feature or module that consumes the
interval.

### 2.2 Raw parser and validation callables

Repository callers submit mappings, not preconstructed nested Pydantic objects.
This prevents framework validation from escaping before typed issues and
idempotency are established.

src/familytrade/strategies/validation.py exports:

~~~python
CalendarKey = tuple[str, int]

def validate_rule_definition(
    value: Mapping[str, object],
    *,
    owner_user_id: str,
    execution_interval_seconds: int,
    fill_interval_seconds: int,
    calendar_versions: Mapping[CalendarKey, CalendarVersion],
) -> DefinitionValidationResult: ...

def canonical_definition_bytes(definition: RuleDefinition) -> bytes: ...

def canonical_definition_sha256(definition: RuleDefinition) -> str: ...

def canonical_operation_request_bytes(value: Mapping[str, object]) -> bytes: ...
~~~

validate_rule_definition is the sole public raw definition parser. It performs a
bounded raw walk before constructing the strict models. It collects all
independently observable issues, retains structurally valid partial records for
later phases, and constructs RuleDefinition only when no issue remains. It never
evaluates features, market data, setups, orders, fills, or lanes.

Validation phases and ordering are normative:

1. Field presence, unknown fields, discriminator, primitive type, decimal syntax,
   and scalar range.
2. Containing-record cross-field rules.
3. Reference, graph, type, unit, operator, and calendar rules.
4. Whole-definition size, count, depth, lookback, and module-combination rules.

Within a phase, issues sort by JSON Pointer path and then code. Duplicate
path/code pairs collapse to one issue. A record with a missing or unknown
discriminator reports the discriminator issue and is not guessed as another
variant. Later phases inspect only fields needed for that rule that parsed
successfully. Thus unrelated valid records still produce semantic findings. The
frozen invalid fixture must return, in order:

~~~text
/nodes/0/offset                        OUT_OF_RANGE
/order_policy/limit_price_source      REQUIRED_FOR_LIMIT
/nodes/7/args                          UNIT_MISMATCH
~~~

Pydantic error normalization is exact:

| Pydantic/raw condition | ValidationIssue code |
| --- | --- |
| extra field | UNKNOWN_FIELD |
| missing field or missing discriminator | REQUIRED |
| wrong JSON primitive, including bool for integer | INVALID_TYPE |
| unknown discriminator or enum member | INVALID_ENUM |
| string/list/numeric bound failure | OUT_OF_RANGE |
| invalid decimal grammar | INVALID_DECIMAL |
| non-finite value | NONFINITE |

Union wrapper locations and internal model class names are never included in
paths or messages. Paths address only the submitted operation document.

ValidationIssue is exactly
{path: JSON-Pointer, code: stable-code, message: string}.
DefinitionValidationResult is exactly:

~~~text
{
  valid:boolean,
  errors:tuple[ValidationIssue,...],
  definition:RuleDefinition|null,
  canonical_definition_sha256:lowercase-sha256|null,
  required_warmup_bars:int|null
}
~~~

A valid result has empty errors and all three derived values. An invalid result
has nonempty errors and null derived values.

### 2.3 Repository callables

src/familytrade/strategies/repository.py exports strategy_metadata and:

~~~python
class StrategyRepository:
    def __init__(
        self,
        engine: Engine,
        *,
        access_repository: AccessRepository,
    ) -> None: ...

    def create_draft(
        self,
        context: UserContext,
        value: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> StrategyVersion: ...

    def edit_draft(
        self,
        context: UserContext,
        value: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> StrategyVersion: ...

    def validate_draft(
        self,
        context: UserContext,
        value: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> StrategyValidationResult: ...

    def get_version(
        self,
        context: UserContext,
        strategy_version_id: str,
    ) -> StrategyVersion: ...

    def list_versions(
        self,
        context: UserContext,
        value: Mapping[str, object],
    ) -> StrategyPage: ...
~~~

The raw operation parser applies the same normalization table to every operation
model. create_draft and edit_draft return VALIDATION_ERROR with
details={"errors":[ValidationIssue JSON objects]} for invalid input and create or
change no strategy row. validate_draft returns
StrategyValidationResult(valid=False, errors=..., strategy_version=None) if a
previously stored draft defensively fails definition validation. Invalid
validate/list operation envelopes themselves raise VALIDATION_ERROR.

Expected operation failures use familytrade.access.models.AccessError and
ErrorCode: UNAUTHENTICATED, INSUFFICIENT_SCOPE, NOT_FOUND, VALIDATION_ERROR,
CONFLICT, IDEMPOTENCY_CONFLICT, and STALE_VERSION. Cross-owner opaque strategy,
source-version, and calendar IDs are NOT_FOUND without disclosing whether they
exist.

StrategyVersion contains exactly schema_version, strategy_version_id,
owner_user_id, name, status, definition_schema_version, definition,
canonical_definition_sha256, catalogue_version, execution_interval_seconds,
fill_interval_seconds, required_warmup_bars, created_from_version_id, created_at,
and record_version. StrategyListItem omits only definition. StrategyPage is
{schema_version:"v1", items:tuple[StrategyListItem,...],
next_cursor:null|string}.

List order is created_at descending then strategy_version_id descending. Its
base64url-without-padding cursor is canonical JSON:

~~~text
{
  v:1,
  owner_sha256,
  operation:"strategy.list",
  status,
  order:"created_at_desc_strategy_version_id_desc",
  after_created_at,
  after_strategy_version_id
}
~~~

owner_sha256 is SHA-256 of context.user_id UTF-8 bytes. All fields are required
and unknown fields are rejected. The cursor is only a keyset position; every query
still has the owner predicate.

## 3. Exact RuleDefinition and bounded records

RuleDefinition is exactly the frozen section 13.2 rule_strategy_v1 record. No
omitted field receives a semantic default except create-input fill interval.
Unknown fields are rejected at every level. Null is accepted only where frozen
shape says null is allowed.

The fixed limits are: 64 KiB canonical definition JSON, 32 feature instances, 128
nodes, 64 condition leaves, 16 arguments or children, group depth 4, arithmetic
depth 4, and 2,000 derived execution bars. IDs are unique; references are acyclic;
offset is integer 0..2000. All roots and filter roots resolve to boolean.

Entry, exit, price-source, stop, target, setup-module, side, combination, order,
sizing, and risk records retain the exact shapes and bounds in contracts-v1
sections 5 and 13. In particular:

- side_policy is long, short, or both. Disabled-side roots are null.
- rules_only, setups_only, setup_and_rules, and setup_or_rules require their
  corresponding roots and enabled emitting setups.
- Reversal has only filter_root at entry intent. Breakout has independent
  arm_filter_root and entry_filter_root.
- Setup expiry and entry-order TTL are separate integers 1..1000.
- Entry windows restrict entries only and never imply position liquidation.
- Every position is protected by bracket_exit_v1. Exit roots supplement it.
- measured_move, next_zone, and r_multiple are supported for both sides.
- Setup-price fields require an enabled setup that emits that field.
- Runtime target selection, tick rounding, fills, risk calculation, and target
  failure outcomes are FT-07 and are not implemented here.

## 4. Closed feature, value, unit, and operator vocabulary

### 4.1 Feature catalogue

The only feature names and derived signatures in feature-catalogue-v1 are:

| Name | kind | output type | unit | exact parameters |
| --- | --- | --- | --- | --- |
| open, high, low, close, hl2, typical | input | price | contract_price | {} |
| volume | input | volume | contract_volume | {} |
| sma_v1, ema_v1 | indicator | type of input | unit of input | {n:int[2..500], input:open|high|low|close|hl2|typical|volume} |
| rsi_wilder_v1 | indicator | decimal | ratio_0_100 | {n:int[2..500], input:"close"} |
| atr_wilder_v1 | indicator | price | contract_price | {n:int[2..500]} |
| relative_volume_v1 | indicator | decimal | ratio | {n:int[2..500]} |
| session_vwap_v1 | indicator | price | contract_price | {} |
| confirmed_pivot_v1 | structure | level | contract_price | {pivot_kind:"high"|"low", left:int[1..50], right:int[1..50]} |
| swing_regime_v1 | structure | regime | regime | {left:int[1..50], right:int[1..50]} |
| prior_session_high_v1, prior_session_low_v1 | level | level | contract_price | {} |
| rolling_high_v1, rolling_low_v1 | level | level | contract_price | {n:int[2..500]} |
| level_touch_v1 | level | boolean | boolean | {level_feature_id, tolerance_ticks:int[0..100]} |
| level_cross | level | boolean | boolean | {level_feature_id, direction:"above"|"below"} |

The supported value_type and unit pairs are closed:

| value_type | legal unit | legal constant representation |
| --- | --- | --- |
| decimal | scalar, ratio, ratio_0_100 | canonical decimal string |
| integer | count | JSON integer, never bool |
| boolean | boolean | JSON boolean |
| price | contract_price | canonical decimal string |
| volume | contract_volume | canonical nonnegative decimal string |
| timestamp | utc_timestamp | RFC3339 UTC string ending Z |
| side | side | "long" or "short" |
| regime | regime | "bullish", "bearish", "sideways", or "unknown" |
| level | contract_price | canonical decimal string |

No other value type, unit, feature name, or parameter is accepted. ratio_0_100
constants are bounded 0..100 inclusive. contract_volume constants are nonnegative.
Other numeric constants need only be finite and canonical unless a consuming
field has a narrower frozen bound. price and level intentionally remain different
types even though both use contract_price.

### 4.2 Finite type algebra

Numeric pairs are decimal with scalar/ratio/ratio_0_100, integer with count, price
with contract_price, volume with contract_volume, and level with contract_price.
An operand pair means both value type and unit.

| operation | accepted operands | derived result |
| --- | --- | --- |
| add, subtract, min, max | two or more identical numeric pairs | that same pair |
| multiply | exactly two operands, at least one decimal/scalar, and the other numeric | the non-scalar pair, or decimal/scalar |
| divide | exactly two operands; denominator decimal/scalar | numerator pair |
| divide | exactly two identical numeric pairs | decimal/scalar |
| eq | two identical pairs of any value type | boolean/boolean |
| lt, lte, gte, gt | two identical numeric pairs, or two timestamp/utc_timestamp | boolean/boolean |
| within | identical numeric left/right and identical nonnegative numeric tolerance | boolean/boolean |
| crosses_above, crosses_below | two offset-zero FeatureNodes with identical numeric pairs | boolean/boolean |
| all, any, none | one to sixteen boolean/boolean children | boolean/boolean |

multiply with zero or more than one non-scalar pair is UNIT_MISMATCH. divide with
any other arity or denominator is TYPE_MISMATCH or UNIT_MISMATCH at args.
Arithmetic on boolean, timestamp, side, or regime is TYPE_MISMATCH. Ordered
comparison on boolean, side, or regime is TYPE_MISMATCH. A mismatch of value type
is TYPE_MISMATCH; when types match but units differ it is UNIT_MISMATCH.
The declared ArithmeticNode result_type and unit must equal the derived pair.

within uses absolute numeric distance and never performs a unit conversion.
Temporal equality is the frozen previous-inclusive/current-strict rule. Current
equality does not cross.

## 5. Deterministic numerical warm-up and data-dependent readiness

required_warmup_bars is derived solely from the validated immutable definition and
its intervals. It is the earliest numerical execution-bar boundary at which all
reachable configured primitives could be ready. It does not promise that required
market structures or calendar data actually exist.

For each feature, derive a minimum history span in seconds:

| Feature | minimum span |
| --- | ---: |
| OHLC, volume, hl2, typical | 1 times its interval |
| sma_v1(n), ema_v1(n) | n times its interval |
| rsi_wilder_v1(n), relative_volume_v1(n) | (n+1) times its interval |
| atr_wilder_v1(n) | n times its interval |
| session_vwap_v1 | 1 times its interval |
| confirmed_pivot_v1(L,R) | (L+R+1) times its interval |
| swing_regime_v1(L,R) | (L+2R+2) times its interval |
| prior_session_high_v1, prior_session_low_v1 | 1 times its interval |
| rolling_high_v1(n), rolling_low_v1(n) | (n+1) times its interval |
| level_touch_v1 depending on D | max(1 times own interval, span(D)) |
| level_cross depending on D | max(2 times own interval, span(D)+interval(D)) |

The swing formula is the earliest possible two confirmed highs and two confirmed
lows: a bar may confirm both kinds, and a second same-kind candidate can first
occur R+1 bars later. Equality, insufficient pivots, and actual price paths remain
data dependent.

For a FeatureNode with offset o, add o times that feature's interval to its span.
A TemporalCompareNode adds one interval of each referenced feature to that
feature's own span; its two operands must have offset zero. Arithmetic, compare,
and group nodes contribute the maximum span of reachable children.

Every entry/exit root, setup filter root, price source, ATR stop source, and enabled
module dependency is reachable. Unreferenced features are still structurally
validated but do not inflate warm-up.

Enabled setup modules add:

- confirmed_pivot_zones_v1:
  max(atr_length times zone interval when use_atr is true, otherwise one zone
  interval; (pivot_left+pivot_right+1) times zone interval).
- reversal_setup_v1:
  one execution interval; two when require_directional_approach is true; and
  peak_lookback execution intervals when recent_peak_stop is true.
- breakout_retest_v1:
  one execution interval in beyond mode and two in strict_cross mode.
- one_position_v1: zero.

The maximum reachable span is converted once as
ceil(span_seconds / execution_interval_seconds). The result must be 0..2000;
otherwise LOOKBACK_LIMIT. This ordering avoids repeated ceiling inflation across
mixed intervals. The frozen crossover fixture derives six bars: slow SMA span five
execution bars plus one slow-feature interval for temporal comparison.

The following do not change the persisted integer and may keep evaluation UNKNOWN
after numerical warm-up:

- Missing or invalid consecutive bars.
- A zero relative-volume denominator or zero VWAP volume.
- No preceding complete stored session.
- Fewer than the required confirmed pivots, equal swing evidence, or no qualifying
  level/zone.
- Fewer actual touches than minimum_touches.
- ATR-on or recent-peak data that is absent or invalid.
- A referenced calendar version whose materialized coverage does not include the
  later run time.

FT-07 owns runtime UNKNOWN reasons and evaluation. FT-06 tests only the deterministic
integer and the fact that the integer is not an availability promise.

## 6. Owner-scoped stored-calendar binding

EntryWindow is exactly:

~~~text
{
  days_of_week:tuple[unique int 1..7, length 1..7],
  start_local:"HH:MM:SS",
  end_local:"HH:MM:SS",
  calendar_id:lowercase UUIDv7,
  calendar_version:int>=1
}
~~~

days_of_week uses ISO local weekdays, Monday 1 through Sunday 7, and is stored
sorted. start_local must be earlier than end_local. A local range that crosses
midnight is represented by two windows; this does not flatten or otherwise limit
an overnight position.

src/familytrade/market_data/catalog.py adds the narrow read-only integration seam:

~~~python
def load_owned_calendar_version(
    connection: Connection,
    owner_user_id: str,
    calendar_id: str,
    calendar_version: int,
) -> CalendarVersion: ...
~~~

It runs on the caller's SQLAlchemy Connection, selects both
market_data_calendar_versions and ordered market_data_calendar_windows with all
three owner/id/version predicates, and returns the existing CalendarVersion model.
No engine checkout, idempotency row, or context callback occurs. If no owned row
matches, it raises the integrated not_found() AccessError. Existing
MarketDataCatalog._calendar delegates to this function, preserving FT-05 behavior.

For create/edit/validate, StrategyRepository raw-scans every structurally valid
distinct (calendar_id, calendar_version) reference, calls this function with
context.user_id and its current transaction connection, and supplies the complete
mapping to validate_rule_definition. A missing and a cross-owner calendar both
produce NOT_FOUND and disclose no calendar fields. A malformed calendar reference
instead remains a normal ValidationIssue and triggers no lookup.

For each supplied window, validation verifies the returned calendar owner, ID, and
version match the key, its requested local interval is represented within the
materialized coverage, and each represented occurrence lies wholly within one
stored kind="open" segment. An occurrence spanning a maintenance/scheduled-closed
segment, two open segments, or uncovered materialized time is INVALID_WINDOW at
/constraints/entry_windows/{index}. A selected local weekday with no occurrence
because it is a stored scheduled closure contributes no eligibility and is not by
itself invalid. An empty entry_windows tuple performs no lookup and means every
stored open segment in the later RunSpec calendar.

Validation uses the calendar's exchange_timezone only to compare local labels
against its stored UTC windows. It never synthesizes a session, infers a weekday
schedule, or changes the stored calendar. Runtime uses those stored UTC segments.

## 7. Canonical bytes and operation request hashes

Canonical RuleDefinition bytes are UTF-8 JSON with NFC strings, lexically sorted
object keys, no insignificant whitespace, preserved array order, and non-ASCII
characters emitted directly. Decimal strings use ordinary base-10 notation, no
exponent or leading plus, at least one integer digit, no leading integer zeroes,
no trailing fractional zeroes, and negative zero normalized to "0".

canonical_definition_sha256 is lowercase SHA-256 of only those typed definition
bytes. It excludes owner, IDs, status, timestamps, record version, catalogue
version, and interval columns. PostgreSQL JSONB key ordering does not change it;
readback reconstructs the typed definition and rechecks the hash.

Duplicate JSON object keys are rejected by the transport decoder before repository
entry and have no idempotent outcome. A direct Python Mapping cannot represent
duplicates.

canonical_operation_request_bytes handles the raw, duplicate-free JSON value graph
before strict model construction. It requires string object keys and only null,
bool, int, finite float, string, array/tuple, and mapping values; normalizes every
string to NFC; sorts keys; preserves array order; and uses Python 3.14 json.dumps
with ensure_ascii=False, sort_keys=True, separators=(",",":"), and
allow_nan=False, encoded as UTF-8. This request canonicalization does not make a
float valid in a decimal-string field; it only gives invalid but JSON-shaped input
a deterministic idempotency identity.

Invalid JSON, duplicate keys, non-string mapping keys, non-finite numbers,
non-JSON Python objects, excessive raw nesting, and an invalid idempotency UUID
fail the hashability/key gate with VALIDATION_ERROR and are not stored or replayed.
Where a repository Mapping supplies a path-addressable non-finite or non-JSON
value, details.errors contains the corresponding NONFINITE or INVALID_TYPE
ValidationIssue even though that pre-identity failure is not cached.
All hashable structural and semantic validation failures occur after the
idempotency lock and are stored and replayed as described below.

## 8. Lifecycle, ownership, idempotency, and concurrency

Create and edit validate the complete raw input before changing a strategy row.
A draft is a valid mutable snapshot. Edit retains its ID, replaces the complete
editable snapshot, recomputes definition hash and warm-up, and increments
record_version. Editing a validated row is CONFLICT.

Creating from an owned validated source produces a new draft ID and same-owner
created_from_version_id. Validate re-runs validation and atomically changes only
status, record_version, and internal updated_at. Successful validation makes the
same ID immutable. Defensive validation failure leaves the draft unchanged and
returns StrategyValidationResult(valid=False). Running lanes remain pinned to old
IDs; FT-06 neither queries nor mutates a lane.

### 8.1 Same-connection FT-04 seam

src/familytrade/access/repository.py adds:

~~~python
def context_is_current_on_connection(
    self,
    connection: Connection,
    context: UserContext,
) -> bool: ...
~~~

It executes the exact existing context_is_current predicates on the supplied
connection: matching auth_session_id and user_id; matching session/context/user
credential version; matching session/context/user administrator value; exact
session scopes equal to the sorted context scopes; enabled user; unrevoked session;
and database clock before both idle and absolute expiry. It opens no connection
and takes no row lock. Existing context_is_current opens its own connection and
delegates to the new method, so its public behavior is unchanged.

StrategyRepository uses only context_is_current_on_connection. A false result is
UNAUTHENTICATED. After a true result it checks strategy:read or strategy:write in
context.scopes; missing scope is INSUFFICIENT_SCOPE. A changed live scope snapshot
causes the context-current predicate to fail before the local scope check.

FT-06 does not add strategy operations to AccessService.authorize_browser_write.
That current FT-04 allowlist has only account, credential, password, logout, and
disable operations. Browser CSRF/Origin and MCP route adapters remain later work.

### 8.2 Mutation transaction and lock order

An idempotency key is any RFC UUID string and is normalized to lowercase.
Idempotency identity is
(owner_user_id, operation, normalized_idempotency_key). request_sha256 is SHA-256
of canonical_operation_request_bytes for the complete operation value, excluding
the separately supplied key.

Each mutation uses one engine.begin connection for its entire attempt:

1. Check current context and required scope on that connection.
2. Validate/normalize the idempotency key and derive raw request bytes/hash.
3. Obtain a transaction-scoped PostgreSQL advisory lock. Its signed big-endian
   64-bit key is the first eight bytes of SHA-256 over canonical UTF-8 JSON
   [owner_user_id, operation, normalized_idempotency_key].
4. Recheck current context and scope on the same connection after the advisory
   lock wait.
5. Read the owner/operation/key idempotency row. A complete same-hash result or
   safe error replays before expected-version or calendar evaluation. A different
   hash is IDEMPOTENCY_CONFLICT.
6. With no row, begin a savepoint. Parse raw input and, when structurally possible,
   load owner calendars on this same connection.
7. For edit/validate, lock the owner-scoped strategy row FOR UPDATE, then recheck
   context and scope on the same connection after that wait, before inspecting
   expected_version or mutating.
8. Apply the domain mutation. Insert exactly one complete result row before outer
   commit.
9. For a safe operation error, roll back the savepoint, insert one complete error
   row in the outer transaction, commit, and raise only after commit.

The order is advisory lock, then strategy row lock. Authentication and calendar
reads take no row locks. No code waits on a strategy row and then requests an
advisory lock. The post-wait read at PostgreSQL READ COMMITTED observes a revocation,
expiry, credential-version change, administrator change, or session-scope change
that committed during the wait.

Authentication and scope failures are never cached. Hashability/key-gate failures
are never cached. Stored safe errors are VALIDATION_ERROR, NOT_FOUND, CONFLICT, and
STALE_VERSION. IDEMPOTENCY_CONFLICT preserves the existing outcome. Internal and
dependency failures are not cached. validate_draft valid=False is a normal result
and is cached as that result.

Same key/request replays the original success or stored safe failure. Same key with
different request bytes is IDEMPOTENCY_CONFLICT. Two different keys editing the
same expected version yield one success and one stored STALE_VERSION. A crash before
outer commit leaves neither domain effect nor idempotency row.

Tests must run mutation waits with a QueuePool configured pool_size=1 and
max_overflow=0. No path may check out a second connection while holding the first.

## 9. PostgreSQL schema and migration

strategy_metadata imports access_metadata and the exact integrated users Table
object and fails import if that binding is inconsistent.

strategy_versions has:

| column | PostgreSQL type |
| --- | --- |
| owner_user_id | varchar(36) NOT NULL |
| strategy_version_id | varchar(36) NOT NULL |
| schema_version | varchar(8) NOT NULL |
| name | varchar(120) NOT NULL |
| status | varchar(16) NOT NULL |
| definition_schema_version | varchar(64) NOT NULL |
| definition | jsonb NOT NULL |
| canonical_definition_sha256 | char(64) NOT NULL |
| catalogue_version | varchar(64) NOT NULL |
| execution_interval_seconds | integer NOT NULL |
| fill_interval_seconds | integer NOT NULL |
| required_warmup_bars | integer NOT NULL |
| created_from_version_id | varchar(36) NULL |
| created_at | timestamptz NOT NULL |
| updated_at | timestamptz NOT NULL, internal only |
| record_version | integer NOT NULL |

The composite primary key is owner_user_id plus strategy_version_id. Every
owner-bearing table has a direct FK using users.c.user_id. created_from uses an
owner/source composite FK. Named checks enforce v1/status/record version/name,
lowercase SHA-256, allowed/divisible intervals, definition JSON object, warm-up
0..2000, and lowercase UUIDv7 IDs. The keyset index is
ix_strategy_versions_owner_status_created_id on
(owner_user_id, status, created_at DESC, strategy_version_id DESC). Definition
hash is not unique.

strategy_idempotency_records has owner_user_id varchar(36), operation varchar(40),
idempotency_key varchar(36), request_sha256 char(64), nullable result jsonb,
nullable error jsonb, and created_at timestamptz. The composite primary key is
owner, operation, and key. Checks allow only the three mutation operations,
lowercase UUID/hash, and exactly one result or error. Stored error JSON is exactly
{code,message,http_status,retryable,details}; request_id is reconstructed from the
current call when raising and is not cached.

The one migration is
migrations/versions/20260914_0003_ft06_strategy_definitions.py, revision
20260914_0003, down_revision 20260913_0002. It creates only the two strategy
tables, named constraints/index, strategy_version_immutability_guard(), and trigger
strategy_versions_immutability. The trigger permits only the exact draft-to-
validated transition fields and rejects every delete or later validated update.
Downgrade removes trigger/function and strategy tables in dependency order while
preserving all FT-04/FT-05 tables and rows. Upgrade-downgrade-upgrade must have no
metadata drift.

Server UUIDv7 and database clock_timestamp() supply IDs and stored time.

## 10. Presets and frozen fixture mapping

src/familytrade/strategies/presets.py exports:

~~~python
def get_preset(preset_id: str) -> StrategyDraftFromDefinitionInput: ...
~~~

Accepted IDs are reversal_breakout_mgc_original_v1,
breakout_mgc_original_v1, and
reversal_breakout_funded_v2_reference_v1. Every call returns a fresh frozen model.
The exact original/funded parameter inventory and provenance labels are those in
the reviewed source documents. Presets are not live chart facts or profitability
claims. The breakout-only preset changes only reversal.enabled to false.

Fixture mappings use docs/contracts-examples-v1.json bytes, never prototype output:

| Requested case | FT-06 assertion |
| --- | --- |
| fully_serialized_rule_definition_crossover | Round-trip, warm-up 6, deterministic hash, and accept the non-preset market/bracket definition. No execution. |
| invalid_rule_definition_has_typed_errors | Return exactly the three ordered path/code issues in section 2 and persist no strategy version. |
| strategy_edit_creates_version_and_running_lane_stays_pinned | Clone/edit/validate a new ID, preserve old row/hash, and keep setup expiry 12 separate from order TTL 3. Lane fields remain fixture evidence only. |
| entry_order_multi_bar_ttl_expiry | Accept and round-trip TTL 2 separately from setup expiry. Expiry/fill outcomes remain FT-07. |
| next_zone_targets_long_short_and_missing | Accept both-side next_zone and no-fallback representation. Target selection and NO_TARGET_ZONE execution remain FT-07. |
| r_multiple_targets_long_short_and_missing | No frozen case has this ID. Map only the exact r_multiple_targets_long_short fixture; do not rename frozen bytes or fabricate a missing branch. Add adversarial absent/nonpositive/out-of-range validation separately. |
| generic_market_entry_resolves_relative_bracket | Accept and round-trip the market fixed-tick stop/risk-multiple target template. Resolution and risk remain FT-07. |

## 11. Exact allowed paths, tests, and validation

The complete implementation path allowlist is:

- docs/ft06-implementation-contract-v1.md, immutable after identical-byte approval.
- src/familytrade/strategies/definitions.py.
- src/familytrade/strategies/validation.py.
- src/familytrade/strategies/repository.py.
- src/familytrade/strategies/presets.py.
- migrations/versions/20260914_0003_ft06_strategy_definitions.py.
- tests/strategies/test_definitions.py.
- src/familytrade/access/repository.py, only the same-connection method and delegation.
- tests/access/test_identity_sessions.py, only same-connection regression coverage.
- src/familytrade/market_data/catalog.py, only the owner-calendar loader and existing
  _calendar delegation.
- tests/market_data/test_models_catalog.py, only owner-calendar loader regression
  coverage.
- migrations/env.py, only strategy_metadata import/target tuple.
- tests/market_data/test_migrations.py, only combined metadata/head preservation.
- .github/workflows/ci.yml, only strategy lint/format/focused pytest additions to
  the existing PostgreSQL market-data job.

No other path is allowed without returning to contract review. In particular,
there is no broader access/market-data refactor, API/UI/MCP route, indicator engine,
fill/lane/backtest/simulation behavior, provider/network code, eval/exec, arbitrary
rule language, optimization, workflow graph, deployment, purchase, or order work.

All named tests in tests/strategies/test_definitions.py:

- test_fully_serialized_rule_definition_crossover_round_trips_and_has_warmup_six
- test_canonical_hash_sorts_keys_normalizes_nfc_and_decimal_strings
- test_canonical_hash_preserves_array_order_and_excludes_version_metadata
- test_raw_unknown_fields_types_and_union_discriminators_map_to_stable_issues
- test_raw_hashable_structural_validation_failure_is_idempotently_replayed
- test_unhashable_or_nonfinite_raw_input_is_rejected_without_idempotency_row
- test_invalid_rule_definition_fixture_returns_all_three_typed_errors
- test_duplicate_ids_unknown_references_cycles_and_non_boolean_roots_are_rejected
- test_node_feature_leaf_group_arithmetic_depth_size_and_lookback_limits_are_inclusive
- test_closed_value_type_unit_and_constant_vocabulary_is_exhaustive
- test_type_unit_operator_matrix_is_exhaustive
- test_temporal_compare_adds_one_feature_interval_and_requires_offset_zero
- test_every_feature_has_deterministic_numeric_warmup
- test_swing_regime_earliest_numeric_warmup_is_l_plus_two_r_plus_two
- test_prior_session_and_pivot_readiness_can_remain_unknown_after_numeric_warmup
- test_mixed_feature_intervals_convert_once_without_ceiling_inflation
- test_feature_and_fill_intervals_divide_execution_interval
- test_long_short_and_both_require_only_matching_roots
- test_rules_setups_and_combination_modes_require_exact_inputs
- test_reversal_entry_filter_and_breakout_arm_entry_filters_are_distinct
- test_setup_expiry_and_one_or_multi_bar_order_ttl_are_independent
- test_owner_calendar_is_loaded_for_each_distinct_entry_window_key
- test_calendar_missing_and_cross_owner_are_indistinguishable_not_found
- test_entry_window_rejects_break_closure_and_unrepresented_segment
- test_empty_entry_windows_needs_no_calendar_and_does_not_imply_flattening
- test_setup_price_sources_require_an_enabled_emitting_setup
- test_generic_market_relative_bracket_fixture_is_valid_without_resolving_prices
- test_next_zone_fixture_binds_both_sides_without_selecting_a_zone
- test_r_multiple_targets_long_short_fixture_uses_exact_existing_id
- test_r_multiple_rejects_absent_nonpositive_or_out_of_range_without_fixture_alias
- test_presets_match_original_breakout_only_and_funded_source_inventories
- test_create_draft_derives_owner_uuid_time_hash_and_warmup
- test_create_or_edit_invalid_definition_changes_no_strategy_row
- test_clone_requires_owned_validated_source_and_sets_same_owner_lineage
- test_edit_draft_replaces_snapshot_and_increments_record_version
- test_validate_transitions_same_id_and_database_prevents_later_update_or_delete
- test_strategy_edit_fixture_preserves_original_and_separate_ttls
- test_same_idempotency_key_same_raw_request_replays_success_and_safe_failure
- test_same_idempotency_key_different_raw_request_is_idempotency_conflict
- test_same_key_concurrent_create_edit_and_validate_commit_one_complete_outcome
- test_safe_error_rolls_back_savepoint_then_commits_replayable_error
- test_context_and_scope_are_rechecked_after_advisory_and_row_lock_waits
- test_pool_size_one_mutations_never_checkout_a_second_connection
- test_revocation_expiry_credential_and_scope_change_during_wait_are_unauthenticated
- test_concurrent_edits_with_same_expected_version_have_one_success_one_stale
- test_read_and_write_scopes_are_enforced_for_every_operation
- test_cross_user_clone_edit_validate_get_and_cursor_are_not_found
- test_list_is_owner_scoped_stably_ordered_bounded_and_cursor_bound
- test_strategy_metadata_owner_fks_bind_exact_access_users_column_object
- test_strategy_migration_upgrade_downgrade_upgrade_preserves_ft04_ft05_rows
- test_combined_access_market_data_strategy_metadata_has_no_duplicate_keys_or_drift
- test_validated_row_trigger_allows_only_exact_draft_to_validated_transition

The narrow dependency regressions are:

- tests/access/test_identity_sessions.py:
  test_context_is_current_on_supplied_connection_matches_existing_public_method.
- tests/market_data/test_models_catalog.py:
  test_load_owned_calendar_version_uses_owner_id_version_and_supplied_connection.
- tests/market_data/test_migrations.py:
  update the existing metadata/drift and downgrade/upgrade tests to include
  strategy_metadata while preserving all existing assertions.

Run from the exact base with a disposable PostgreSQL 16 database in
FAMILYTRADE_TEST_DATABASE_URL:

~~~text
uv sync --frozen --all-groups
uv run pytest tests/strategies/test_definitions.py
uv run pytest tests/access tests/market_data tests/strategies/test_definitions.py
uv run ruff check src/familytrade/strategies tests/strategies src/familytrade/access/repository.py tests/access/test_identity_sessions.py src/familytrade/market_data/catalog.py tests/market_data/test_models_catalog.py migrations/versions/20260914_0003_ft06_strategy_definitions.py migrations/env.py tests/market_data/test_migrations.py
uv run ruff format --check src/familytrade/strategies tests/strategies src/familytrade/access/repository.py tests/access/test_identity_sessions.py src/familytrade/market_data/catalog.py tests/market_data/test_models_catalog.py migrations/versions/20260914_0003_ft06_strategy_definitions.py migrations/env.py tests/market_data/test_migrations.py
uv run mypy src
~~~

The strategy test performs Alembic upgrade head, downgrade 20260913_0002, and
upgrade head plus compare_metadata against the disposable database. The combined
command is the required FT-04/FT-05 regression readback. CI runs the focused
strategy file in the existing PostgreSQL market-data job; it adds no service/job.

Fixture-boundary tests assert only FT-06-owned expected fields. They must not build
target calculation, order clocks, fills, risk/P&L, lane state, indicator evaluation,
setup transitions, or a miniature FT-07 engine.

## 12. Dispatch terminal

Implementation must return WAIT_CONTRACT_GAP if the exact base exposes a conflicting
field, formula, lifecycle, ownership call, migration path, fixture expectation, or
required path outside this allowlist. Private helper naming alone is routine and
does not reopen planning.

Both fresh independent reviewers must read the frozen sources and integrated
FT-04/FT-05 APIs, verify this exact file fingerprint, and PASS identical bytes
before code dispatch. Every byte change requires both reviews again. The
implementation worker remains gpt-5.6-terra / medium; Reviewer A remains
gpt-5.6-terra / medium; Reviewer B remains gpt-5.6-sol / high.
