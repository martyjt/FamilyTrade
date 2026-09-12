# FamilyTrade first-slice contracts v1

Status: **FT-02 frozen candidate for independent review**. This is a planning
contract, not implementation, provider proof, performance evidence or authority to
trade. The companion hand-calculated fixtures are
[`contracts-examples-v1.json`](contracts-examples-v1.json). A review record and
content digests must be added outside this file after two independent reviewers pass
the same frozen bytes.

This contract adopts the user's accepted bounded rule builder, configurable trading
behaviour, overnight positions, paper controls and verified automatic recovery. Its
recommended values are initial paper-research defaults. Every trading policy called
configurable below remains a saved field; a named preset never becomes an engine
restriction. Actual provider contract facts, fees, permissions, margin and live risk
limits remain commissioning evidence.

## 1. Authority, implementation choices and evolution

The first implementation uses Python with FastAPI, Pydantic v2, SQLAlchemy 2,
Alembic and psycopg 3; PostgreSQL is authoritative for operational state. Parquet is
the immutable candle archive. The browser uses React, TypeScript, Vite, TanStack
Router/Query, React Hook Form and generated OpenAPI types. Zod validates forms at the
browser boundary; Pydantic remains authoritative. AG Charts renders charts. FT-01
pins mutually compatible maintained versions without changing these contracts.

JSON Schema 2020-12 describes stored definitions and public operation payloads.
Every document has `schema_version`; v1 readers reject an unknown major version and
ignore only documented additive fields in a known major version. Stored events are
append-only. Migrations preserve old readers during a release overlap and never
reinterpret previously stored decisions, fills or pinned datasets.

Recommended authentication components are `pwdlib[argon2]` using
`PasswordHash.recommended()` for local passwords, opaque database-backed browser
sessions, and Authlib OAuth primitives plus the maintained MCP Python SDK for the
remote MCP authorization/resource boundary. The MCP endpoint targets the current
`2026-07-28` stateless Streamable HTTP protocol, OAuth 2.1 protections, Protected
Resource Metadata, Authorization Server Metadata, Authorization Code with S256 PKCE,
issuer binding and short-lived bearer access tokens. FT-01 must prove the pinned SDK
supports those requirements; it must not silently fall back to the removed session
handshake or legacy HTTP+SSE. Relevant maintained references are the
[MCP authorization specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization),
[MCP transport specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http),
[FastAPI security guidance](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/)
and [Authlib authorization-server guidance](https://docs.authlib.org/en/stable/oauth2/authorization-server/).
The [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
documents stable v2 support for the 2026-07-28 and earlier revisions. FT-01 pins a
compatible v2 release and performs dependency import/schema checks; FT-15 owns an
authenticated real-client interoperability test. No bespoke or unsafe fallback is
allowed when a required authorization or protocol capability is absent.

## 2. Normative representation and error envelope

`MUST`, `MUST NOT`, `SHOULD` and `MAY` are normative. IDs are server-generated
lowercase UUIDv7 strings unless an explicitly defined deterministic ID is used.
Hashes are lowercase SHA-256 hex. Timestamps are RFC 3339 UTC strings ending `Z`,
with at most six fractional digits. Calendar-local labels are never timestamps.
Durations are integer seconds; bar intervals are positive integer seconds. Counts
and quantities are integers. Prices, money, rates, indicator values and multipliers
are finite base-10 strings, never JSON floats. Currency money is quantized to the
currency minor unit after each fill; intermediate P&L uses at least 12 decimal
digits and round-half-even only at the money boundary.

An operation succeeds with `{schema_version, request_id, result}` or fails with
`{schema_version, request_id, error:{code,message,retryable,details}}`. Stable codes
include `UNAUTHENTICATED` (401), `INSUFFICIENT_SCOPE` (403), `NOT_FOUND` (404),
`CONFLICT` (409), `IDEMPOTENCY_CONFLICT` (409), `VALIDATION_ERROR` (422),
`STALE_VERSION` (409), `RATE_LIMITED` (429), `DEPENDENCY_UNAVAILABLE` (503),
`STALE_DATA` (409), `LEASE_LOST` (409) and `INTERNAL_ERROR` (500). A resource owned
by another user returns `NOT_FOUND`, without confirming its existence. Error details
contain field paths and public values only; credentials, provider payloads and
cross-user identifiers are excluded.

Mutation requests require a UUID `idempotency_key` and, when updating existing
state, `expected_version`. Reusing a key with the same authenticated user, operation
and canonical request returns the original outcome. Reusing it with different bytes
returns `IDEMPOTENCY_CONFLICT`. Caller-supplied owner IDs never establish authority.

## 3. Core entity contracts

Except for the request-only UserContext, persisted entities require `schema_version`,
their primary ID, `owner_user_id`, `created_at` and integer `record_version >= 1`.
`owner_user_id` is
written from `UserContext` and is never accepted as an operation argument.

### 3.1 UserContext

Required fields are `schema_version`, `user_id`, `auth_session_id`, `auth_method` (`browser_session` or
`mcp_oauth`), sorted unique `scopes`, `authenticated_at`, `expires_at`, `request_id`
and `credential_version`. It is an in-memory request value, not user-editable JSON.
`expires_at > authenticated_at`; a disabled user, revoked session/token, expired
credential or missing scope yields no context. Invalid examples are a context made
from `?user_id=`, a bearer token minted for another resource, or a context reused
after revocation.

### 3.2 BrokerAccount

Required fields are `broker_account_id`, `owner_user_id`, `provider`,
`provider_account_ref_ciphertext_id`, `environment` (`paper` or `live`), `status`
(`pending_verification`, `active`, `reauth_required`, `disabled`), sorted
`capabilities`, `credential_envelope_id`, `verified_at|null`, `last_reconciled_at|null`,
`created_at`, `updated_at` and `record_version`. Provider account references are
masked on output. A credential envelope belongs to exactly one user/account/provider
purpose. The v1 paper engine does not require a broker account. `live` cannot become
`active` without FT-03 proof. Invalid: shared credential envelopes, plaintext secret
fields, or using a paper account as proof of independent virtual-lane accounting.

### 3.3 FuturesContract

Required fields are `contract_id`, `provider`, `provider_contract_id`, `root_symbol`,
`exchange`, `currency`, `tick_size`, `multiplier`, `expiry_label`,
`first_trade_at|null`, `last_trade_at`, `exchange_timezone` (IANA), `calendar_id`,
`calendar_version`, `entry_cutoff_at`, `liquidation_start_at`, `metadata_as_of`,
`provenance_ref` and `record_version`. `tick_size` and `multiplier` are positive;
all executable prices are tick multiples; `entry_cutoff_at < liquidation_start_at <
last_trade_at`. The provider ID names an actual expiry. Root-only and continuous
symbols are invalid for paper execution. Fixture values are synthetic; FT-03 must
replace them with authoritative dated-contract facts.

Recommended expiry defaults, configurable per contract master, block new entries at
the start of the third complete exchange trading day before `last_trade_at` and begin
liquidation at the start of the final complete trading day before `last_trade_at`.
The versioned calendar materializes the resulting UTC instants; code does not infer
them from weekdays.

### 3.4 CompletedBar

Required fields are `bar_record_id`, `owner_user_id`, `source`, `price_basis`, `contract_id`,
`interval_seconds`, `start_at`, `end_at`, `open`, `high`, `low`, `close`, `volume`,
`source_revision`, `received_at`, `completed_at`, `quality`, `supersedes_bar_record_id|null`
and `payload_hash`. Logical identity is `(owner_user_id, source, price_basis, contract_id,
interval_seconds, start_at)`; every correction creates a new `bar_record_id` and
increments `source_revision`. V1 accepts only `price_basis:"trades"`; midpoint,
bid, ask and settlement series are rejected rather than mixed. `end_at = start_at + interval_seconds`; OHLC prices are
tick multiples; `low <= open,close <= high`; volume is a nonnegative integral
decimal string in contracts; quality is one of
`valid`, `missing`, `invalid`, `duplicate_conflict`. Only `valid` is strategy-eligible.
A developing bar is a separate ephemeral type and can never be serialized as a
CompletedBar. Invalid: a fabricated zero-volume gap bar, a local timestamp, or an
in-place correction.

### 3.5 DatasetRevision

Required fields are `dataset_revision_id`, `owner_user_id`, `series_key`,
`parent_revision_id|null`, `manifest_uri`, `manifest_sha256`, ordered `partition_refs`,
`calendar_id`, `calendar_version`, `coverage_start`, `coverage_end`,
`source_watermark`, ordered `correction_refs`, `status` (`building`, `published`,
`quarantined`), `created_at` and `published_at|null`. `series_key` is the owner,
source, price basis, actual contract and interval tuple. A published revision and every referenced
object are immutable. Coverage is `[coverage_start,coverage_end)`. A pinned read
names exactly one published revision; `latest` is a query policy, not a stored fake
revision. Invalid: a manifest crossing users or sources, a mutable path, missing
checksum, or a revision referring to an unverified calendar version.

### 3.6 StrategyVersion

Required fields are `strategy_version_id`, `owner_user_id`, `name`, `status`
(`draft` or `validated`), `definition_schema_version`, `definition`,
`canonical_definition_sha256`, `catalogue_version`, `execution_interval_seconds`,
`fill_interval_seconds`, `required_warmup_bars`, `created_from_version_id|null`,
`created_at` and `record_version`. Validation canonicalizes object keys and decimal
strings before hashing. A validated version is immutable; editing creates a new ID.
`fill_interval_seconds` defaults to 60, divides the execution interval and is not
larger than it. Running lanes remain pinned. Invalid: expression text, arbitrary
code, an unknown node, an unsupported parameter, or a future/negative offset.
The exact `definition` tagged shape is in section 13.2; a worker may not invent a
different callable shape.

### 3.7 RunSpec

Required fields are `run_id`, `owner_user_id`, `mode` (`backtest` or
`forward_paper`), `strategy_version_id`, `contract_id`, `dataset_policy`,
`start_at`, `end_at|null`, `starting_cash`, `base_currency`, `cost_model`,
`fill_model`, `sizing_policy`, `risk_policy`, `entry_windows`, `end_policy`,
`engine_version`, `random_seed|null`, `submitted_at` and `record_version`.
Backtests require `dataset_policy={mode:"pinned",dataset_revision_id}` and a finite
`end_at`. Forward paper requires `mode:"causal_latest"`; every consumed bar record
and resulting checkpoint still records its exact revision. `end_policy` is
`mark_open` (default) or `force_close`; forward paper only permits `mark_open`.
Randomness is absent in v1 fill semantics; a seed is required only by a declared
research feature. All limits must be at least as restrictive as account caps.
`run.submit` accepts a `RunSubmitInput` projection containing every RunSpec field
except server-generated `run_id`, `owner_user_id`, `submitted_at` and
`record_version`; the response supplies those fields. A backtest spans at most five
calendar years and defaults to at most 500,000 canonical bars, with an administrator
hard ceiling of 2,000,000; larger work is rejected or explicitly split into bounded
runs.

### 3.8 Decision

Required fields are `decision_id`, `owner_user_id`, `run_id`, `lane_id|null`,
`strategy_version_id`, `dataset_revision_id|null`, `source_bar_record_ids`,
`execution_bar_end`, `decision_sequence`, `decision_type`, `side|null`,
`setup_id|null`, `reason_code`, `evidence`, `order_intent|null`,
`pre_state_sha256`, `post_state_sha256`, `causation_event_id`, `effective_at`,
`decided_at` and
`idempotency_key`. The deterministic uniqueness key is `(run_or_lane,
decision_sequence, strategy_version_id, execution_bar_end)`. Evidence records exact
feature version/value/source/`known_at`. A decision cannot cite a bar ending after
`execution_bar_end`. In a backtest, `effective_at = execution_bar_end`. In forward
paper, `effective_at` is the latest causal availability time of all source bars and
is therefore `>= execution_bar_end`; `decided_at >= effective_at` is the durable
record time and `submitted_at >= decided_at` for any order. The execution-bar label
never moves merely because delivery was delayed.
A backtest Decision requires its pinned published `dataset_revision_id`. A forward
Decision records the latest published base revision when one exists, or null while
it consumes newer active rows; in both cases exact `source_bar_record_ids` are the
authoritative immutable evidence. Later archive publication never patches a Decision.
Invalid: mutable prose as the sole reason, missing source bars, or a second outcome
for the same uniqueness key.

### 3.9 Fill

Required fields are `fill_id`, `owner_user_id`, `run_id`, `lane_id|null`, `order_id`,
`contract_id`, `bar_record_id`, `fill_sequence`, `side` (`buy` or `sell`), `effect`
(`open` or `close`), `quantity`, `base_price`, `fill_price`, `slippage`,
`commission`, `currency`, `model_time`, `realized_pnl`, `cash_after`,
`position_quantity_after`, `position_average_after|null`, `reason`,
`causation_decision_id|null`, `created_at` and `fencing_token`. Quantity is positive
and a v1 fill is complete, never partial. The deterministic uniqueness key is
`(run_or_lane,order_id,fill_sequence)`. `model_time` is the eligible fill bar start
for an opening gap and its end for an intrabar touch. Invalid: a fill before order
activation, a worse-than-limit fill, or a stale fencing token.

### 3.10 RunEvent

Required fields are `run_event_id`, `owner_user_id`, `aggregate_type`, `aggregate_id`,
`sequence`, `event_type`, `effective_at`, `recorded_at`, `payload`,
`causation_id|null`, `correlation_id`, `state_sha256`, `attempt_id|null` and
`fencing_token|null`. `(aggregate_id,sequence)` and `run_event_id` are unique.
Events are append-only and ordered by sequence, not wall-clock arrival. Payloads use
typed event schemas. Replaying the same cause returns the existing event; a different
payload at the same sequence is `CONFLICT`.

## 4. Time, bars, sessions and corrections

Bars cover `[start_at,end_at)`. In a backtest, a pinned valid historical bar is
modeled as available exactly at `end_at`; its later ingestion `received_at` is not
used as simulated market causality, and the result is labelled historical. In
forward paper, a bar is usable only after the provider completion has been durably
received, so its availability is `max(end_at,completed_at,received_at)`. A decision
at `T` may cite only valid bars with `end_at <= T` that were available by that
decision's modeled effective time. One-minute bars are canonical. Aggregation intervals supported in v1
are zone `{60,300,900}` seconds and execution `{300,900,1800,3600}` seconds; the
smaller interval must divide the larger.

The versioned exchange calendar is authoritative. It contains explicit UTC session
segments, maintenance breaks, closures, early closes and a `trading_day` label for
each segment. IANA rules help produce and audit the schedule; runtime mapping uses
the stored UTC schedule. A DST fold or gap therefore creates neither duplicate nor
missing UTC bars. A trading day can cross midnight. No bar spans a session segment
or maintenance break.

Aggregate buckets anchor at each segment start. A derived bar is `valid` only when
every expected canonical bar is present and valid. At a scheduled segment end, a
short final bucket may be published only with `partial_policy:"scheduled_partial"`;
the default strategy policy rejects it. Unscheduled partials remain missing. OHLC is
first/max/min/last and volume is the exact sum in timestamp order.

Corrections append a CompletedBar and become correction refs in the next child
DatasetRevision when published. New unpinned
research reads select the highest published source revision per logical bar.
Completed runs remain pinned to their old revision. A forward lane uses the exact
bar versions already committed: a correction to an already processed bar records
`CORRECTION_OBSERVED` with `applied:false` and reason `LATE_AFTER_CURSOR`, but does not rewrite state, decisions, cash or fills and
does not recompute the indicator chain. A correction received before processing is
eligible. Historical reconstruction is labelled `reconstructed`; it is never
presented as contemporaneous paper evidence.

## 5. Bounded rule builder and feature catalogue

### 5.1 Types, graph and limits

Feature types are `decimal`, `integer`, `boolean`, `price`, `volume`, `timestamp`,
`side`, `regime` and `level`. Units must match; a price cannot be compared with a
unitless decimal without an explicit supported conversion. Nodes are `feature`,
`constant`, `arithmetic` (`add`, `subtract`, `multiply`, `divide`, `min`, `max`),
`compare`, `temporal_compare`, and `group` (`all`, `any`, `none`). Division by zero
is `UNKNOWN`. Comparators are `lt`, `lte`, `eq`, `gte`, `gt`, `within`,
`crosses_above`, and `crosses_below`. Decimal equality is exact after declared
quantization; price equality is exact tick equality.

Each definition is at most 64 KiB canonical JSON, 32 distinct feature instances,
64 condition leaves, 16 children per group, group depth 4, arithmetic depth 4 and
2,000 bars of total declared lookback. Every node has a unique ID. A historical
offset is integer `0..2000`; zero means the current completed evaluation bar.
Future references, recursion, cycles, dynamic field names, unbounded windows and
caller expressions are invalid. Conditions use three-valued results
`PASS|FAIL|UNKNOWN`; an entry/exit rule requires `PASS`, and `UNKNOWN` records a
reason rather than using an older value.

For `all`, any `FAIL` yields `FAIL`, all `PASS` yields `PASS`, otherwise `UNKNOWN`.
For `any`, any `PASS` yields `PASS`, all `FAIL` yields `FAIL`, otherwise `UNKNOWN`.
For `none`, any `PASS` yields `FAIL`, all `FAIL` yields `PASS`, otherwise `UNKNOWN`.
Empty groups are invalid. Node records, references and unit algebra are frozen in
section 13.2.

`crosses_above(a,b)` is `previous(a) <= previous(b) and current(a) > current(b)`;
`crosses_below` is `previous(a) >= previous(b) and current(a) < current(b)`.
Equality on the previous bar is therefore allowed, equality on the current bar does
not cross. Rule groups short-circuit only computationally; persisted evidence lists
every leaf result in lexical node-ID order.

### 5.2 Inputs and indicator equations

Supported bar inputs are `open`, `high`, `low`, `close`, `volume`, `hl2=(high+low)/2`
and `typical=(high+low+close)/3`. Supported lengths are `2..500` unless stated.
Missing/invalid bars break consecutive warm-up; no feature reaches across a gap.

- `sma_v1(n,x)`: arithmetic mean of the latest `n` completed values including the
  current value. Warm-up is `n`.
- `ema_v1(n,x)`: seed at value `n` with `sma_v1(n,x)`; thereafter
  `EMA_t = (2/(n+1))*x_t + (1-2/(n+1))*EMA_(t-1)`. Warm-up is `n`.
- `rsi_wilder_v1(n,close)`: require `n+1` closes. Seed average gain/loss from the
  first `n` deltas, then update each as `(previous*(n-1)+current)/n`.
  RSI is `100-100/(1+avg_gain/avg_loss)`; both zero gives 50, zero loss gives 100,
  and zero gain gives 0.
- `atr_wilder_v1(n)`: true range is `max(high-low,abs(high-previous_close),
  abs(low-previous_close))`, using `high-low` for the first available bar. Seed with
  the arithmetic mean of `n` consecutive true ranges, then Wilder-update as above.
- `relative_volume_v1(n)`: current completed volume divided by the arithmetic mean
  of the **previous** `n` completed volumes. It requires `n+1` bars; a zero mean is
  `UNKNOWN`. Bound `n=2..500`.
- `session_vwap_v1`: from the first canonical bar in the stored exchange session,
  `sum(typical*volume)/sum(volume)`, reset at each trading-day session start. A zero
  cumulative volume is `UNKNOWN`; session breaks do not reset it.

Feature arithmetic uses IEEE-style decimal context precision 34, exponent range at
least decimal128, and round-half-even. Each indicator output and recurrent state is
quantized to 12 decimal places after its formula step; subsequent steps consume that
quantized state. Overflow, non-finite input or an inexact value outside the supported
exponent range is `UNKNOWN`. Prices derived for orders use the tick rules in section
6 and money uses currency quantization, so neither is forced through feature display
precision.

### 5.3 Market structure and levels

`confirmed_pivot_v1(L,R)` uses `L,R=1..50`. A high at index `k` is `>=` every
previous `L` high and `>` every next `R` high; lows are symmetric (`<=` left, `<`
right). It becomes available only after bar `k+R` completes. Time-distinct equal
prices are distinct events. `confirmed_pivot_zones_v1` follows the reviewed
Reversal–Breakout draft, with bounds: ATR `2..500`; positive finite zone multipliers
`(0,100]`; minimum touches `1..100000`; retained zones `2..200`; age `1..100000`
zone bars; cooldown `0..1000` execution bars.

`swing_regime_v1` uses the last two confirmed pivot highs and last two confirmed
pivot lows: both strictly higher is `bullish`, both strictly lower is `bearish`, a
mixed pair is `sideways`, and insufficient/equal evidence is `unknown`. Its
`known_at` is the latest of the four confirmation times.

`prior_session_high_v1` and `prior_session_low_v1` use all valid canonical bars from
the immediately preceding complete exchange trading session and become available at
the current session start. A missing prior session is `UNKNOWN`.
`rolling_high_v1(n)`/`rolling_low_v1(n)` use exactly the previous `n` completed bars,
excluding the current bar, with `n=2..500`. `level_touch_v1` is true when the current
bar range intersects `[level-tolerance,level+tolerance]`; tolerance is a nonnegative
tick multiple bounded to 100 ticks. `level_cross` uses completed closes and the
crossover equality rule.

Detailed volume-at-price, aggressor delta, order-book imbalance, arbitrary scripts
and unconfirmed pivots are outside v1. ADX and any new primitive require a separately
reviewed formula/version before they can be enabled. The Reversal–Breakout preset's
raw proximity/beyond-level modes, optional directional approach/strict cross,
filter stages, setup expiry, entry TTL, target/stop modes and both sides remain the
configurable policies recorded in `strategies/configurability-v1.md`. Its parameter
bounds remain authoritative where narrower than the generic bounds here.

### 5.4 Strategy and risk configuration bounds

Supported execution/zone intervals are those above. Setup expiry and entry TTL are
separate integers `1..1000` execution bars; the recommended Reversal–Breakout preset
uses 24 and 1. Sides are `long`, `short`, or `both`; strategy families and gates are
independently enabled. Target mode is `measured_move`, `next_zone`, or `r_multiple`.
All finite positive strategy multipliers are `(0,100]`; daily entries are `1..1000`.
The first slice still enforces one entry order and one position per lane.

Supported sizing is `fixed_contracts` (`quantity 1..100`, default 1) or
`stop_risk_fraction` (`fraction (0,0.05]`, default `0.005`; maximum quantity
`1..100`). Supported cost settings are commission `0..1000` currency units per
contract per side and slippage `0..100` ticks by order class. Supported money limits
are positive and at most `1000000000`; percentage limits are `(0,1]`. Account caps
override looser strategy/run fields and cannot be disabled from a strategy.

### 5.5 Setup target modes

Target modes freeze their immutable zones, multipliers and feature inputs at the
signal boundary. When the executable entry is already known, target price freezes
there; a fill-relative market template resolves atomically from the prospective
entry under section 13.2. Direction is `+1` long and `-1` short, and final prices use
section 6 rounding.

- `measured_move`: a breakout's origin is its frozen break edge and impulse is
  `abs(arm_close-edge)`. A reversal's origin is its entry; its impulse is the price
  distance to the nearest eligible opposing-zone boundary selected by the next-zone
  rule below. Raw target is `origin + direction*impulse*measured_move_multiple`.
- `next_zone`: long selects the eligible zone with `zone.low > executable_entry`
  having the smallest `zone.low-entry`; target is that `zone.low`. Short selects
  `zone.high < entry` with the smallest `entry-zone.high`; target is `zone.high`.
  Distance ties choose newer zone creation sequence, then lexical zone ID. The zone
  must be confirmed and known at the signal time; later zones cannot alter the
  frozen target.
- `r_multiple`: price risk `R = abs(executable_entry-executable_stop)` after entry
  and stop tick rounding, excluding fees and potential gap slippage. Raw target is
  `entry + direction*R*r_multiple`; for a market template these values are the
  prospective fill and atomically resolved stop, not a guessed signal price.

After target rounding, long must satisfy `stop < entry < target` and short
`target < entry < stop`. Nonpositive impulse/R, no eligible opposite/next zone, or
invalid rounded ordering rejects the candidate with `NO_TARGET_ZONE`,
`INVALID_TARGET_DISTANCE`, or `INVALID_BRACKET` respectively. No target fallback is
allowed. `next_zone` and `r_multiple` are configurable non-default policies; the
Reversal–Breakout source-near preset retains its reviewed measured-move settings.

## 6. Order activation, fills, ticks and accounting

### 6.1 Order model and timing

Supported entry orders are `market` and `limit`; the Reversal–Breakout preset uses
`limit`. Protective brackets are a stop-market plus target-limit OCO pair. Quantity
is all-or-none in the paper model; partial fills, queue position and market impact
are not claimed. Each order has `submitted_at`, `active_from`, `expires_at`, status,
and an owner lane/run. For a backtest, a decision effective at execution-bar end `T`
submits at modeled time `T` and activates at the first valid fill bar whose
`start_at >= T`. For forward paper, `submitted_at` is the durable wall-clock commit;
activation is the first fill bar whose `start_at >= submitted_at` (rounding up to the
next boundary when processing occurred after a bar began). Thus a delayed live
decision cannot retrospectively use an already-open bar. It can never fill on the
signal bar. The order stores both modeled `effective_at` and actual `submitted_at`.

TTL is counted in execution bars while fill evaluation uses canonical one-minute
bars by default. With a 15-minute decision interval and TTL 1, every valid one-minute
bar in `[T,T+15m)` is eligible; unresolved entry orders expire at `T+15m` before a
decision at that boundary. Missing bars do not extend wall-clock expiry. A scheduled
market closure suspends eligibility but not an absolute contract cutoff. Configurable
TTL changes StrategyVersion; the preset value is not hard-coded.

Recommended paper costs are one adverse tick for market and stop-market fills, zero
ticks for a touched limit, and `1.25` currency units per contract per fill side. The
`1.25` is a synthetic paper assumption, **not an IBKR or exchange fee quote**. Every
run stores its chosen cost model.

### 6.2 Tick rounding

Raw strategy prices become executable prices once, when the order is created:

| Role | Long | Short |
| --- | --- | --- |
| Entry limit | buy: floor to tick | sell: ceiling to tick |
| Protective stop | sell: ceiling to tick | buy: floor to tick |
| Profit target | sell: floor to tick | buy: ceiling to tick |

These directions keep an entry limit from becoming more aggressive and move
protective/target levels toward the entry rather than silently increasing risk or
reward. A rounded stop must remain strictly beyond the entry in the loss direction
and a target in the profit direction; otherwise validation rejects the setup.

### 6.3 Fill rules for both sides

Evaluate eligible one-minute bars in time order. For a position whose bracket and
market close intent were active before the bar starts: (1) resolve a bracket trigger
already crossed by the opening gap; (2) if none, fill the market close at the open
with adverse market slippage and cancel the bracket; (3) if no close intent, evaluate
intrabar bracket touches and ambiguity; then (4) only while flat evaluate entries.
Thus a later intrabar target cannot preempt a close that filled at the open, while a
protective opening gap is honored before that close. Existing positions cannot be
reversed by a same-bar entry. For a buy market order, base is bar open and fill is
`open + slippage_ticks*tick`; sell is `open - ...`.

A buy limit fills on an opening gap when `open <= limit`, with base `open` and fill
`min(limit,open+slippage)`; otherwise it fills when `low <= limit`, at the limit.
A sell limit is symmetric: opening gap `open >= limit`, fill
`max(limit,open-slippage)`; otherwise `high >= limit`, at the limit. A limit never
fills worse than its limit.

For a long position's sell stop, an opening gap `open <= stop` uses base `open`;
otherwise `low <= stop` uses base `stop`; fill subtracts adverse stop slippage. For
a short position's buy stop, opening gap `open >= stop` or intrabar `high >= stop`
uses the symmetric base and adds slippage. A long sell target fills at a favorable
gap open when `open >= target`, otherwise at target if `high >= target`. A short buy
target is symmetric. Target limits receive price improvement at the gap open and no
slippage in v1.

For a bracket active before the bar starts and without an active market close after
the opening-gap phase, the full bar range test applies. If both stop and target are
then reachable, the
default `both_hit_policy:"stop_first"` fills the stop and cancels the target. The
configurable alternative `target_first` exists for sensitivity research and must be
named in RunSpec; it is never silently inferred. Provider tick sequencing is not
claimed.

For a position opened at the bar open, its bracket is active for the remainder of
that bar and the both-hit rule applies. For an entry first reached intrabar, exact
ordering is unknown. Default `entry_bar_exit_policy:"conservative_stop_first"`
applies a same-bar stop if the bar also reaches the stop, never grants a same-bar
target, and otherwise carries the bracket forward. The alternative
`next_bar_only` defers both exits and is retained for sensitivity comparisons; it can
hide a same-bar loss and UI/MCP must label that limitation. It is not the default.

`close_and_stop` creates a market close intent for the next eligible one-minute bar;
it cannot fabricate a fill during stale data or a closure. Entry-window closure,
pause, risk latch and contract cutoff cancel pending entries, not protective exits.
Commission is charged only for a Fill; submission, expiry, rejection and cancellation
have zero commission.

### 6.4 Futures cash, mark and drawdown

Futures entry does not subtract notional value. Each fill deducts
`commission_per_contract_per_side * quantity`. Closing realized P&L is
`direction * (exit-entry) * multiplier * quantity`, where direction is `+1` long and
`-1` short. Cash after close adds realized P&L and deducts exit commission; entry
commission was already deducted. Equity is `cash + direction*(mark-entry)*multiplier
* quantity`. Mark is the latest valid completed fill-interval close known at the
as-of time. If it is stale, retain the last mark with `mark_status:"stale"`; no new
entry or invented mark is allowed.

The default equity/drawdown series is bar-close: starting equity plus equity after all
events and the close mark of each completed canonical fill bar, net of costs. High
water is the maximum series value to that bar; drawdown is `high_water-equity`,
floored at zero, and maximum drawdown is the series maximum. It does not claim an
intrabar worst excursion. Session-end and ordinary run-end do not flatten. A backtest
with `mark_open` ends with an open position, last mark, unrealized P&L and separate
realized P&L. For `force_close`, validation precomputes `force_close_at` as the start
of the last valid fill bar whose end is at or before `end_at`, blocks new entries that
could outlive it and schedules a market close there. It never reaches `end_at` and
backdates an order. If no such bar exists, the RunSpec is invalid; if the preselected
bar later fails integrity, the run is `BLOCKED_UNCLOSED`.

### 6.5 Metrics including empty runs

`net_pnl=end_equity-starting_cash`; `return=net_pnl/starting_cash`; fees are the sum
of Fill commissions; realized and unrealized P&L remain separate; exposure is
position-open eligible-bar seconds divided by eligible run seconds. A trade is one
closed entry-to-flat position. Win rate is winning closed trades divided by closed
trades. Gross profit is the sum of positive trade P&L after allocated entry/exit
fees; gross loss is the absolute sum of negative trade P&L; profit factor is gross
profit/gross loss. With zero closed trades, trade count, fees and realized P&L are
zero, while win rate, average trade and profit factor are `null` with reason
`NO_CLOSED_TRADES`. With trades but zero gross loss, profit factor is `null` with
reason `ZERO_GROSS_LOSS`. A run with no fills has zero exposure, net P&L and return,
and bar-close drawdown zero. Open-position mark, unrealized P&L and drawdown still
apply even when closed-trade metrics are null.

## 7. Sizing, risk, overnight and expiry

The recommended synthetic commissioning profile is starting cash `25000.00`, fixed
quantity 1, maximum one position, per-entry modeled stop risk `125.00` (0.5% of
starting equity), daily loss `250.00` (1%), cumulative drawdown `2500.00` (10%), and
maximum 23 entry fills per trading day. These are conservative paper-research
defaults, configurable within section 5 bounds, and are not suitable evidence for a
live account. Actual margin and broker liquidation are outside the paper model.

For `stop_risk_fraction`, budget is `fraction * min(starting_equity,current_equity)`.
Per-contract modeled stop risk is absolute rounded entry-to-slipped-stop distance times
multiplier plus two commissions. Quantity is `floor(budget/per_contract_risk)`, then
capped by configured and account maximums; zero rejects `RISK_SIZE_ZERO`. Fixed
quantity is rejected when the same total exceeds the per-entry cap. Immediately
before a paper entry Fill, compute its prospective actual fill without committing.
Revalidate long `executable_stop < prospective_fill < executable_target` or short
`executable_target < prospective_fill < executable_stop`. A favorable limit gap or
market gap can invert that bracket; cancel with `ENTRY_GEOMETRY_GAP`, no Fill and no
commission. If geometry remains valid, recompute risk using that prospective fill,
including an opening gap and slippage. If it now exceeds the cap, cancel before any
Fill with `RISK_GAP`; no commission is charged. (A future live order needs broker-side
controls and cannot assume this simulator-only ability.) Risk checks and the
entry-counter increment occur atomically with the entry fill; a race cannot admit a
second position.

This estimate is not a maximum possible loss: a later protective-stop gap can lose
more, as the fixtures demonstrate. Daily-loss and drawdown latches block subsequent
entries but cannot cap gap losses. Fill-time gap inspection is available to the
deterministic paper model because its synthetic bar open is the modeled fill input;
it does not claim that a real market order can know or reject its eventual fill.

At each stored trading-day boundary, `daily_start_equity` is the last marked equity
from the prior day, including carried unrealized P&L. During the day,
`daily_loss=max(0,daily_start_equity-current_marked_equity)`, after all costs. Reaching
the cap (`>=`, inclusive) latches `DAILY_LOSS_LIMIT` until the next trading day even
if price recovers. It cancels entries and setups but keeps protective exits. The
daily entry limit is also inclusive: entry is allowed only when `filled_entries <
limit`; increment once on fill.

Positions, cash, high-water and brackets carry through session breaks and trading-day
boundaries. Session VWAP and the daily entry/loss baseline reset; position basis and
cumulative drawdown do not. Closed-market bars are not invented. At reopen, existing
stops/targets evaluate the first valid bar with the gap rules before new entries.

At `entry_cutoff_at`, cancel entries and block new ones. At `liquidation_start_at`,
cancel entry state and create a persisted close intent. It fills at the next eligible
bar and then the lane stops for that contract. No automatic roll occurs. If data or
market eligibility cannot produce a close before `last_trade_at`, status is
`BLOCKED_EXPIRY_UNRESOLVED`; the position and last mark remain visible and operator
action is required. A backtest lacking coverage through its required liquidation
window is invalid before execution.

## 8. Archive revisions and crash-safe publication

Archive objects are physically separate by user. The default partition is one
`owner/source/actual-contract/interval/trading-day` object. Paths are generated from
opaque IDs, never user text. Objects and manifests are immutable and content hashed.
Publishing a pinned revision first snapshots any required active rows into immutable
objects. Pinned readers never depend on the mutable active table.

Latest reads union the latest published manifest with active PostgreSQL rows and
select the greatest source revision per logical bar; active rows win only when their
source revision is greater. A source-revision tie with a different payload is
`duplicate_conflict` and fails closed. Pinned reads use only manifest references.
Corrections publish replacement objects and a child manifest; old objects remain for
the retention period. Default retention is all revisions referenced by a run/lane or
legal hold, plus unreferenced revisions for 90 days. Deletion requires a separate
audited maintenance job and never crosses user directories.

Publication protocol: (1) write a random temporary file on the destination
filesystem; (2) flush and fsync file, calculate checksum, fsync directory; (3) insert
a PostgreSQL `staged` publication with intended final URI/checksum; (4) atomic rename
to a new immutable final path and fsync directory; (5) in one database transaction
reacquire the valid publication fence, lock the series row, verify checksum/owner
and that the latest pointer still equals the staged parent, mark object and manifest
`published`, and compare-and-swap the latest pointer; (6) cleanup superseded active
rows only after the commit. A lost compare-and-swap rebases to a child revision and
does not publish the stale candidate. Readers
see database `published` objects only and verify checksum before use.

| Crash point | Visible result | Recovery |
| --- | --- | --- |
| Before/during temporary write | No published change | Remove old temp after 24h |
| After fsync, before staged row | No published change | Quarantine unreferenced temp |
| After staged row, before rename | No published change | Retry verified rename or mark quarantined |
| After rename, before publish transaction | No published change | Reconciler obtains a fresh fence, verifies checksum and parent, then completes same staged publication or quarantines |
| During publish transaction | Old latest pointer remains | Transaction rollback; reconcile staged/final object |
| After publish commit, before active cleanup | New revision visible once | Idempotent cleanup; logical-key precedence prevents duplicates |
| Published object missing/corrupt | Read fails `ARCHIVE_INTEGRITY` | Quarantine revision; never fall back silently |
| Correction races publication | One source revision per committed manifest | Loser rebases into a child revision; never mutates winner |

## 9. Durable jobs, schedules, claims and fencing

`Job` requires `{schema_version,job_id,owner_user_id,job_type,payload_ref,state,
priority:int[-100..100],queued_at,available_at,max_attempts,current_attempt_count,
current_attempt_id|null,fencing_token,cancel_requested_at|null,last_error_code|null,
idempotency_key,created_at,updated_at,record_version}`. `job_type` is
`backtest|historical_backfill|gap_repair|archive_publish|quality_check`; its strict
payload is stored by owned immutable reference. `(owner_user_id,idempotency_key)` is
unique.

`Attempt` requires `{schema_version,attempt_id,job_id,owner_user_id,attempt_number,state,
worker_id,fencing_token,claimed_at,started_at|null,heartbeat_at|null,
lease_expires_at,finished_at|null,error_code|null,checkpoint_ref|null,created_at,
record_version}`.
`attempt_number` starts at 1 and is unique per job; its fence equals the claim fence.

`Schedule` requires `{schema_version,schedule_id,owner_user_id,job_type,
payload_template_ref,state,timezone,local_rule,next_fire_at,overlap_policy:
"skip",misfire_grace_seconds:int[0..86400],dst_gap_policy:"skip",
dst_fold_policy:"first_only",last_occurrence_id|null,created_at,updated_at,
record_version}`. `ScheduleOccurrence` requires `{schema_version,occurrence_id,
schedule_id,owner_user_id,scheduled_for,state,job_id|null,reason,created_at,
record_version}` where state is
`ENQUEUED|OVERLAP_SKIPPED|MISFIRE_SKIPPED|DST_GAP_SKIPPED|DST_FOLD_FIRST_ONLY`.
The deterministic uniqueness key is `(schedule_id,scheduled_for)`; `scheduled_for`
is the intended UTC occurrence, or the first UTC instant after a nonexistent local
time solely for the audited DST-gap skip identity.
`local_rule` is exactly `{kind:"daily",days_of_week:unique int[1..7][1..7],
time_local:"HH:MM:SS"}` or `{kind:"interval",seconds:int[60..86400],anchor_at}`;
interval rules advance in UTC and have no fold/gap ambiguity. A partial unique
constraint permits at most one nonterminal Attempt per Job.

Job states are `QUEUED`, `RUNNING`, `CANCEL_REQUESTED`, `SUCCEEDED`, `FAILED_RETRYABLE`,
`EXHAUSTED`, and `CANCELLED`. Attempt states are `CLAIMED`, `RUNNING`, `SUCCEEDED`,
`FAILED`, `LEASE_LOST`, and `CANCELLED`. Legal job transitions are:

```text
QUEUED -> RUNNING
RUNNING -> SUCCEEDED | FAILED_RETRYABLE | EXHAUSTED | CANCEL_REQUESTED
FAILED_RETRYABLE -> QUEUED
QUEUED | RUNNING | FAILED_RETRYABLE -> CANCEL_REQUESTED
CANCEL_REQUESTED -> CANCELLED
```

Attempt transitions are `CLAIMED -> RUNNING -> SUCCEEDED|FAILED|LEASE_LOST|CANCELLED`;
`CLAIMED` may also go directly to `LEASE_LOST|CANCELLED` before work starts.

Terminal states have no outgoing transition. A retry creates a new attempt; it never
reopens an attempt. Default maximum attempts is 3 (`1..10` configurable). Retry
wait after failed attempt 1 is 5 seconds, after attempt 2 is 30 seconds, and after
attempt 3 or any later attempt is 120 seconds. Validation/ownership
failures are immediately exhausted. Cancellation is cooperative at an atomic
checkpoint; no new effect begins after its transaction observes the request.

A claim transaction selects an eligible job with `FOR UPDATE SKIP LOCKED`, verifies
owner resource budgets, increments the job's monotonic `fencing_token`, inserts an
attempt and sets a 45-second lease. The owner heartbeats every 15 seconds in a
transaction. Every state/decision/fill/archive write locks the claim row and verifies
both the current token and `database_clock < lease_expires_at`; a mismatch or expired
lease rejects the whole transaction with `LEASE_LOST`, even before another owner
claims. An expired owner may finish computation but cannot commit. Takeover increments the token before work.
Exactly-once delivery is not claimed; deterministic IDs, unique constraints and
fenced transactions make replay effects idempotent.

Schedules have `ACTIVE|PAUSED|DISABLED`, IANA timezone, explicit local rule, next UTC
fire, overlap policy and misfire policy. Default overlap is `SKIP` with an audited
event. Default misfire is one immediate catch-up if at most 15 minutes late;
otherwise record `MISFIRE_SKIPPED`. A local scheduled time that does not exist in a
DST spring gap records `DST_GAP_SKIPPED`; an ambiguous fall-fold time fires once at
the earlier UTC occurrence and records `DST_FOLD_FIRST_ONLY`. The next UTC fire is
persisted, so restart cannot fire the second fold occurrence. No backlog fan-out occurs. Gap-repair jobs derive
missing ranges from durable coverage rather than replaying every missed schedule.

Firing is one database transaction under the locked Schedule row: compare stored
`next_fire_at`, insert the unique occurrence, either insert exactly one idempotent
Job or the applicable skip result, calculate and persist the next UTC fire, and set
`last_occurrence_id`. A crash rolls back all of those effects. Retry of the same
occurrence returns its existing row and cannot enqueue twice. Overlap means an
earlier nonterminal Job from that schedule exists at fire time. Schedule transitions
are `ACTIVE <-> PAUSED`, `ACTIVE|PAUSED -> DISABLED`; disabled is terminal. An
occurrence is immutable. A pause advances no fire; resume computes the first future
fire and applies the misfire rule once, without backlog fan-out.

The initial global budget is one running backtest worker across the installation and
one running backtest per user, chosen by oldest eligible `queued_at` with a rotating
user tie-break so one user's queue cannot starve another. Default per-user budgets
also allow one provider-maintenance job, 20 queued jobs, 30 minutes wall time per
ordinary backtest, 2 GiB worker memory and two CPU cores. The administrator sets a
global CPU/memory ceiling no higher than measured host capacity; worker admission
must fit both global and user ceilings. Archive repair can have a separately reviewed larger budget. Paper lanes
have dedicated claims, default maximum five per user and twenty globally, and do not consume the backtest slot. Provider pacing
limits are independent and FT-03-supplied. Budget changes are administrator-owned,
audited and bounded; user strategy fields cannot raise them.

## 10. Paper lane state, controls and recovery

Persist `operator_intent` separately as `RUNNING`, `PAUSE_ENTRIES`,
`CLOSE_AND_STOP`, or `STOPPED`. Runtime status is `STARTING`, `ACTIVE`, `PAUSED`,
`WAITING_FRESH_DATA`, `RECOVERING`, `CLOSING`, `CREATED`, `STOPPED`, or `FAULTED`. Lane identity
pins owner, StrategyVersion, actual contract and account/cost/risk policy. A version
change never mutates a lane: start a new lane after the old lane is flat/stopped.
Transferring an open position between strategy versions is unsupported.

Initial start creates no position/order/setup, sets cash and high-water from RunSpec,
zeros cumulative metrics, assigns the stored trading-day baseline/counters, and warms
features without emitting historical entries. Pause cancels pending signals/setups
and entries but preserves feature state, cash, position, marks and counters. Resume
preserves all of those values and waits for the next fresh decision bar. Close-and-stop
preserves them until its Fill updates cash/position; terminal stop retains the final
read-only snapshot. A new-version lane starts a separate simulated account from its
own RunSpec and cannot inherit the old lane's cash, counters, orders or position.

`LaneCreateInput` is `{strategy_version_id,contract_id,starting_cash,base_currency,
cost_model,fill_model,sizing_policy,risk_policy,entry_windows}` and uses the tagged
records from sections 3.7/13.3; create returns a server-owned lane in `CREATED`
without warming or processing. `LaneStartInput` is `{lane_id,expected_version,
idempotency_key}` and is accepted only from `CREATED`; it returns the persisted
starting/waiting state. Every control uses `LaneControlInput={lane_id,action:
"pause_entries"|"resume"|"close_and_stop"|"stop_when_flat",expected_version,
idempotency_key}`.

Every create/start/control/get returns `LaneSnapshot={lane_id,strategy_version_id,
contract_id,record_version,operator_intent,runtime_status,freshness_status,
last_bar_record_id|null,last_execution_bar_end|null,published_base_revision_id|null,
state_sha256,position|null,pending_entry|null,protective_bracket|null,
close_intent|null,cash,equity,mark_status,high_water,daily_start_equity,
filled_entries_in_trading_day,risk_latches,last_error_code|null,created_at,updated_at}`.
The owner is implicit in the authenticated response and never an input. Abbreviated
position/order records reuse Fill/order fields from sections 3.9/13.3 and cannot
change their meaning.

The atomic lane checkpoint transaction contains fencing token, last consumed logical
bar and exact bar record/revision, feature/setup state, orders, position, cash,
marks, high-water, daily baseline/counters, operator intent, status, emitted
decisions/events/fills and the next deterministic sequences. Either all commit or
none. External notifications use an outbox in the same transaction.

Default feed staleness during an open calendar segment is no valid completed
one-minute bar by expected bar end plus 90 seconds. A scheduled break is not stale.
Recovery requires an exclusive fresh fence, checkpoint/hash validation, contiguous
eligible bars from the cursor, no unresolved archive conflict, and latest expected
bar no more than 90 seconds late. Previously `RUNNING` lanes may then recover
automatically. Manual pause/close/stop always wins.

| Requested action | Flat, fresh/open | Open position, fresh/open | Stale or scheduled closed |
| --- | --- | --- | --- |
| Start | Warm indicators through latest complete bar; activate on the next bar | Invalid: a new lane cannot inherit a position | Persist `RUNNING`, status `WAITING_FRESH_DATA`; no decisions |
| Pause new entries / manage exits | Cancel arms/entries, keep indicators warm, status `PAUSED` | Same; keep bracket active | Persist `PAUSE_ENTRIES`; no fabricated cancellation fill |
| Resume | Explicitly set `RUNNING`; evaluate no entry until a fresh next decision bar | Same; exits remain active first | Wait for verified freshness; automatic repair cannot supply intent |
| Close positions and stop | Cancel entry state and stop immediately | Persist close intent; market-close at next eligible bar, then `STOPPED` | Flat: stop immediately. Open: persist `CLOSE_AND_STOP`, status `CLOSING`; remain pending |
| Stop when flat | Stop immediately | Reject with `POSITION_OPEN`; use close-and-stop or pause | Flat stops; open remains unchanged |

While `CLOSING`, a repeated close-and-stop is idempotent. Pause, resume, start and
stop-when-flat are rejected with `CLOSE_PENDING` while a position remains; v1 exposes
no cancel-close action. After the close fills the lane is `STOPPED`, and resume is
invalid; a user creates a new pinned lane. Credential/account disable immediately
blocks entries and recovery, but cannot report a paper position closed.

On process loss, a new owner replays only after lease expiry and token increment. It
loads the checkpoint and reprocesses later bars. Existing deterministic decisions,
fills and events deduplicate. A crash before the checkpoint commit leaves no effect;
a crash after commit resumes after its cursor. A stale owner cannot commit. Feed
repair may append missing research bars but never creates entry decisions whose order
activation would have been in the past. During recovery, missed bars advance
indicator/session/risk state with `recovery_only:true` and suppress new entries.
For an already-open paper position, protective brackets and a previously persisted
close intent are resolved across those missed bars using section 6, because they
existed at the historical time; resulting fills carry
`evidence_mode:"reconstructed_after_outage"`, original modeled time and later
recorded time. They are reported separately from contemporaneous paper evidence.
If required bars are missing or conflicting, recovery remains blocked rather than
guessing an exit. Already-processed late corrections follow section 4. At reopen
after an overnight break, resolve existing exits and close intents before entries.

## 11. Identity, credentials and shared UI/MCP operations

Local accounts are administrator-invited. Passwords use pwdlib's current recommended
Argon2 settings and a dummy-hash path prevents username timing enumeration. Browser
login creates a random 256-bit opaque token; only its hash is stored. Cookie settings
are `Secure`, `HttpOnly`, `SameSite=Lax`, host-only and path `/`; state-changing
requests require a bound CSRF token and Origin check. Default idle expiry is 12 hours,
absolute expiry 7 days, with rotation on login/privilege change and revocation on
disable/password change.

MCP access tokens default to 15 minutes; refresh tokens, if issued, rotate and are
stored hashed. Initial clients are administrator pre-registered or use Client ID
Metadata Documents; deprecated Dynamic Client Registration is disabled. Scopes are
`data:read`, `strategy:read`, `strategy:write`, `runs:read`, `runs:write`,
`lanes:read`, `lanes:control`. Protected Resource Metadata identifies one issuer;
tokens are issuer/resource/audience/scope/expiry checked. No provider credential is
accepted or returned through MCP tools.

Provider credentials use envelope encryption: a random data-encryption key per
credential, authenticated encryption, a deployment key-encryption key supplied
outside Git/images/database, purpose and owner bound as associated data, and
versioned rotation. Outputs expose status and last four/masked reference only.
Secrets and tokens never enter logs, events, fixtures, URLs or exception details.
Credential create/update accepts write-only secret bytes over HTTPS. Update writes
and verifies a new envelope, atomically increments credential/account version and
revokes the old envelope; failure leaves the old version active. Delete revokes the
envelope, disables the BrokerAccount and invalidates provider sessions, while
retaining non-secret audit metadata. Revoked/unavailable credentials yield
`DEPENDENCY_UNAVAILABLE` and block reconnect; they are never replaced with empty
values. These account operations are administrator/browser operations in v1 and are
not exposed as MCP tools.

UI and MCP call the same application operations and schemas:

| Operation | Scope | Input beyond request/idempotency metadata | Result |
| --- | --- | --- | --- |
| `data.coverage` | `data:read` | contract, interval, optional pinned revision | owned coverage, gaps, quality, revision |
| `data.bars` | `data:read` | contract, interval, revision, `[start,end)`, cursor, limit | at most 5,000 owned bars plus next cursor |
| `strategy.list` / `strategy.get` | `strategy:read` | cursor/limit or owned version ID | metadata page or exact typed definition |
| `strategy.create_draft` | `strategy:write` | name, definition or source version | new draft version |
| `strategy.edit_draft` | `strategy:write` | draft ID, expected version, full replacement definition | incremented draft snapshot |
| `strategy.validate` | `strategy:write` | owned draft ID, expected version | typed errors or immutable validated version |
| `run.submit` | `runs:write` | RunSubmitInput | server-completed owned RunSpec plus run/job IDs |
| `run.get` / `run.compare` | `runs:read` | owned run IDs, bounded metrics | state/results/evidence |
| `run.decisions` / `run.fills` | `runs:read` | run ID, cursor, limit, filters | at most 500 ordered records plus next cursor |
| `run.cancel` | `runs:write` | run ID, expected version | durable cancellation state |
| `lane.create` | `lanes:control` | LaneCreateInput | `CREATED` LaneSnapshot |
| `lane.start` | `lanes:control` | LaneStartInput | starting/waiting LaneSnapshot |
| `lane.pause_entries` | `lanes:control` | LaneControlInput with `pause_entries` | persisted intent/snapshot |
| `lane.resume` | `lanes:control` | LaneControlInput with `resume` | persisted intent or waiting reason |
| `lane.close_and_stop` | `lanes:control` | LaneControlInput with `close_and_stop` | persisted close intent/snapshot |
| `lane.stop_when_flat` | `lanes:control` | LaneControlInput with `stop_when_flat` | terminal snapshot or `POSITION_OPEN` |
| `lane.get` | `lanes:read` | lane ID | state, freshness, position, pending actions |

Lists are cursor paginated, default 50 and maximum 200 unless a smaller table limit
is stated. Cursors bind owner, operation, filters and ordering and cannot be reused
with different arguments. Comparison accepts 2..20 runs. One call cannot act for several users. The UI does not receive a more powerful
owner selector than MCP. Authentication failure is 401; valid identity without scope
is 403; another user's opaque ID is 404. Every successful control reports persisted
intent, not an assumed fill or eventual status.

## 12. Routine release classes, budgets and rollback

Routine classes are `STATIC_UI`, `API`, `WORKER`, and `SCHEMA_EXPAND`. `STATIC_UI`
and `API` leave ingestion and workers untouched. `WORKER` uses fenced graceful
handoff. `SCHEMA_EXPAND` is compatible with old and new code. Schema contraction,
PostgreSQL/host/provider-gateway maintenance, secrets rotation and host reboot are
planned maintenance, not routine application release.

Initial accepted recommendation budgets are:

| Measure | Initial budget |
| --- | ---: |
| Public authenticated HTTP maximum contiguous unavailability | `<= 5s` |
| Current MCP authenticated tool maximum contiguous unavailability | `<= 10s` |
| Graceful API drain | `<= 30s` |
| Per-lane processing gap during worker release | `<= 90s` |
| Canonical ingestion publication lag during worker release | `<= 120s` |
| Valid ownership overlap / duplicate committed effects | exactly `0` |
| Later routine-release target | zero user-visible failed requests and no lost/duplicate effects |

These are acceptance budgets to measure on the authorised VPS, not measured claims.
An external probe runs at 2 requests/second from five minutes before first cutover
until ten minutes after final health, reports every response, maximum contiguous
failure time and p50/p95/p99 latency, and permits at most ten failed HTTP probes in
that 15-minute window. Authenticated MCP probes run every second and retry one
in-flight idempotent write after a dropped response; both attempts must resolve to
one persisted outcome. Lane/ingestion evidence records cursors, lag and fence tokens.
Provider-independent release failures and provider/auth failures are reported
separately; a release-caused reconnect still counts.

Readiness must cover schema compatibility, authentication and dependencies before
proxy switch. Failed readiness causes no switch. After switching, drain old API for
30 seconds. Keep the prior image/configuration and expand-compatible schema for a
10-minute rollback window; a failed post-switch critical probe switches traffic back
within the applicable HTTP/MCP budget. Worker owners stop claiming new work, finish
or checkpoint within 30 seconds, relinquish, and replacement owners take a higher
fence. A web-only release does not perform worker handoff. Test open overnight,
paused, stale-feed, close-pending and forced-owner-loss lanes.

Every worker/lane checkpoint envelope carries `format_version`, `written_by_engine`
and state hash. A worker release manifest pins `previous_format`, `current_format`,
`readable_formats` and `write_format`. The replacement must read both previous and
current. Throughout the 10-minute rollback window it writes the previous format, or
a new format proven readable and writable without semantic loss by the old worker;
optional new fields must be safely reconstructible. Readiness runs both directions:
new worker reads/continues an old checkpoint, and the rollback worker reads/continues
a checkpoint written by the candidate, with identical cursor, cash, orders, position,
counters and next deterministic IDs. Failure rejects `WORKER` as an incompatible
routine release before handoff. Promoting an incompatible checkpoint write format
waits until the rollback window ends and is a separately planned migration; it
cannot claim routine rollback to the old worker.

## 13. Serialization, tagged records and version policy

### 13.1 Canonical JSON and projections

Canonical bytes are UTF-8 JSON with lexically sorted object keys, no insignificant
whitespace, Unicode NFC and array order preserved. Decimal strings use ordinary
base-10 notation, no exponent or leading plus, normalize negative zero to `"0"`,
remove leading integer zeroes and trailing fractional zeroes, and retain at least one
integer digit. Hashes use those canonical bytes. Storage rows may normalize fields,
but round-trip output reconstructs the same typed value and canonical hash.

Public create inputs omit entity IDs, owner, timestamps and record version unless a
specific operation says otherwise. Server projections add them from UserContext and
the database clock. Mutation definitions and event payloads reject unknown fields;
response envelopes may add fields within v1. Null is permitted only where the field
explicitly says `|null`; omission and null are distinct.

### 13.2 RuleDefinition and node records

`StrategyVersion.definition` is exactly this top-level record:

```text
{
  kind:"rule_strategy_v1", name:string(1..120), side_policy:"long"|"short"|"both",
  features: FeatureInstance[0..32], nodes: RuleNode[0..128],
  entry_rules:{long_root:node_id|null,short_root:node_id|null},
  exit_rules:{long_root:node_id|null,short_root:node_id|null},
  entry_combination:"rules_only"|"setups_only"|"setup_and_rules"|"setup_or_rules",
  exit_policy:{kind:"bracket_exit_v1",stop:StopSpec,target:TargetSpec},
  setup_modules:SetupModule[0..5],
  order_policy:{entry_type:"market"|"limit",limit_price_source:PriceSource|null,
    entry_ttl_execution_bars:int[1..1000],
    both_hit_policy:"stop_first"|"target_first",
    entry_bar_exit_policy:"conservative_stop_first"|"next_bar_only"},
  constraints:{max_entries_per_trading_day:int[1..1000],entry_windows:Window[]}
}
```

`FeatureInstance` is `{feature_id,kind,name,output_type,unit,interval_seconds,
parameters}`. `feature_id` is unique. `kind` is `input|indicator|structure|level`;
`name` is one catalogue name in section 5; `output_type` and `unit` must equal that
catalogue result; parameters contain only that feature's named bounded parameters.
A `Window` is `{days_of_week:[1..7],start_local:"HH:MM:SS",end_local:"HH:MM:SS",
calendar_id,calendar_version}` and cannot cross an unrepresented session segment.
An empty `entry_windows` means every open segment in that exact calendar version;
it never includes a scheduled closure or maintenance break.

`PriceSource` is `{kind:"feature",feature_id,offset_ticks:int[-100000..100000]}`
or `{kind:"setup_price",field:"entry"}`. A limit entry requires one and a market
entry requires null. A feature source must output price at the signal boundary; its
tick offset is applied and then section 6 rounding is used. `setup_price` is valid
only when an enabled setup module emits its frozen entry.

`StopSpec` is exactly one of `{kind:"setup_price",field:"stop"}`,
`{kind:"fixed_ticks",ticks:int[1..100000]}`, or
`{kind:"atr_multiple",feature_id,multiple:decimal(0,100]}`. `TargetSpec` is exactly
one of `{kind:"setup_price",field:"target"}`, `{kind:"fixed_ticks",ticks:
int[1..100000]}`, or `{kind:"risk_multiple",multiple:decimal(0,100]}`. ATR features
must be `atr_wilder_v1`. Every entry intent freezes both specs and every referenced
feature value. A setup-price spec freezes its absolute raw price at intent. For a
limit entry, the executable limit is known at signal time: fixed-tick/ATR stops and
fixed-tick/risk-multiple targets resolve and tick-round from that limit then, and a
later favorable gap fill does not move them. For a market entry only, those relative
specs are a fill-relative template: after computing the prospective actual entry
fill, long stop raw price is `fill-distance` and short is `fill+distance`, where
distance is `ticks*tick_size` or the frozen ATR times its multiple. A fixed-tick
target is symmetric in the profit direction, and `risk_multiple` resolves from the
resulting rounded entry/stop under section 5.5. Resolve and tick-round the complete
market bracket atomically before committing the entry Fill, then apply geometry and
risk checks; no position can commit unprotected.
Setup-price specs require an enabled setup module that emits that field; generic rule
strategies use fixed-tick or ATR/risk-multiple specs. These market fill-relative semantics
make the valid generic market-plus-risk-multiple configuration deterministic without
using data learned after the prospective fill.

Every `RuleNode` has a unique `node_id` and exactly one tagged shape:

```text
{kind:"feature",node_id,feature_id,offset:int[0..2000]}
{kind:"constant",node_id,value_type,unit,value}
{kind:"arithmetic",node_id,op:"add"|"subtract"|"multiply"|"divide"|"min"|"max",
 args:node_id[2..16],result_type,unit}
{kind:"compare",node_id,op:"lt"|"lte"|"eq"|"gte"|"gt"|"within",
 left:node_id,right:node_id,tolerance:node_id|null}
{kind:"temporal_compare",node_id,op:"crosses_above"|"crosses_below",
 left_feature:node_id,right_feature:node_id}
{kind:"group",node_id,op:"all"|"any"|"none",children:node_id[1..16]}
```

References form an acyclic graph and roots resolve to boolean nodes. Add/subtract,
min/max and ordered comparisons require equal types and units. Multiply permits one
scalar operand; divide by a scalar preserves the numerator unit, and dividing equal
units yields scalar. `within` requires equal left/right units and a nonnegative
tolerance of that unit. Temporal comparisons require two feature nodes with the same
unit and use their current and previous completed outputs; callers cannot construct
an offset that bypasses warm-up. Boolean nodes cannot enter arithmetic. The
validator independently derives every `result_type`/`unit` and rejects a mismatch.

An entry root is evaluated at the completed execution boundary. A configured exit
root is supplementary to the always-required protective bracket: when it becomes
`PASS` for an open matching-side position, persist a market close intent for the
next causally eligible fill bar. The bracket stays active until the close fills and
has section 6 precedence. `UNKNOWN` never closes. Root fields for disabled sides
must be null. `rules_only` requires an entry root and no emitting setup; `setups_only`
requires an emitting setup; the two combination modes require both and apply the
named boolean relationship before normal conflict resolution.

`SetupModule` is a strict tagged record. Supported records are:

```text
{kind:"one_position_v1"}
{kind:"confirmed_pivot_zones_v1",zone_interval_seconds,use_atr,atr_length,
 pivot_left,pivot_right,merge_multiple,max_width_multiple,minimum_touches,
 max_zones,zone_max_age_bars,cooldown_execution_bars}
{kind:"reversal_setup_v1",enabled,sides:"long"|"short"|"both",
 approach_multiple,require_directional_approach,stop_buffer_multiple,
 recent_peak_stop,peak_lookback,target_mode:"measured_move"|"next_zone"|"r_multiple",
 measured_move_multiple,r_multiple,filter_root:node_id|null}
{kind:"breakout_retest_v1",enabled,sides:"long"|"short"|"both",
 confirmation_mode:"beyond"|"strict_cross",break_multiple,pullback_multiple,
 setup_expiry_execution_bars,stop_buffer_multiple,use_vwap_stop,
 target_mode:"measured_move"|"next_zone"|"r_multiple",measured_move_multiple,
 r_multiple,arm_filter_root:node_id|null,entry_filter_root:node_id|null}
```

All `*_multiple` fields are finite decimals `(0,100]`; interval, ATR, pivot, zone,
lookback, expiry and cooldown bounds are sections 5.3-5.4. Disabled setup modules
remain serialized with their parameters for reproducibility but emit no candidate.
Each module kind appears at most once; reversal/breakout require one zones module
and `one_position_v1`. A filter root resolves to boolean; reversal supports only the
entry stage, while breakout supports the separately named arm and entry stages.

Top-level family modules do not narrow configurability: the reviewed
Reversal–Breakout preset uses `confirmed_pivot_zones_v1`, `reversal_setup_v1`,
`breakout_retest_v1` and `one_position_v1`, with setup-price bracket specs, while a simple
crossover can use catalogue features and entry roots without those setup modules.
Unknown modules/combinations are rejected; supported non-preset combinations are
valid when their typed graph and required exit/order fields pass.

### 13.3 Embedded order, cost, manifest and evidence records

`Decision.order_intent`, when present, is
`{order_id,kind:"entry"|"close",order_type:"market"|"limit",side:"buy"|"sell",
effect:"open"|"close",quantity,raw_price:null|decimal,executable_price:null|price,
raw_stop:null|decimal,stop_price:null|price,raw_target:null|decimal,
target_price:null|price,bracket_template:null|{stop:StopSpec,target:TargetSpec,
frozen_feature_values:object},effective_at,submitted_at,active_from,expires_at,
entry_bar_exit_policy,both_hit_policy}`. Entry intent requires a complete bracket;
limit-entry raw/executable price and bracket fields are all present and frozen at
signal time. For a market entry's relative specs only, raw/executable bracket fields
are null until the prospective fill resolves the complete template; absolute setup
fields remain present. Close intent requires bracket fields and template null.
`active_from >= submitted_at` in forward
paper and `>= effective_at` in backtest.

`RunSpec.cost_model` is `{commission_per_contract_per_side,currency,
market_slippage_ticks,stop_slippage_ticks,limit_slippage_ticks}`. `fill_model` is
`{fill_interval_seconds,partial_fill_policy:"all_or_none",both_hit_policy,
entry_bar_exit_policy}`. `sizing_policy` is tagged
`{kind:"fixed_contracts",quantity}` or `{kind:"stop_risk_fraction",fraction,
max_quantity}`. `risk_policy` is `{per_entry_loss_cap,daily_loss_cap,
cumulative_drawdown_cap,max_positions:1,max_entries_per_trading_day}`.

A DatasetRevision `partition_ref` is `{object_id,uri,sha256,byte_length,row_count,
min_start_at,max_end_at,min_source_revision,max_source_revision}`. A correction ref
is `{logical_bar_key,old_bar_record_id,new_bar_record_id,reason,received_at}`.
Manifests list refs sorted by `(min_start_at,object_id)` and contain no overlapping
logical-bar/source-revision pairs.

Decision `evidence` is `{evaluation_stage,results:[{node_id,result:
"PASS"|"FAIL"|"UNKNOWN",value:null|decimal|boolean|string,unit,source_bar_record_ids,
known_at,reason_code}],selected_setup:null|object}` with results in lexical node-ID
order. `known_at <= decided_at`; backtests additionally label evidence
`historical`, forward decisions `contemporaneous`, and outage exits
`reconstructed_after_outage`.

### 13.4 RunEvent payload registry and state transitions

`event_type` must equal `payload.kind`. V1 payloads are strict tagged records:

- `STATE_TRANSITION`: `{kind,from,to,reason,expected_record_version}`.
- `DECISION_RECORDED`: `{kind,decision_id,decision_sequence}`.
- `ORDER_STATE`: `{kind,order_id,from,to,reason}`.
- `FILL_RECORDED`: `{kind,fill_id,order_id,fill_sequence,evidence_mode}`.
- `MARK_RECORDED`: `{kind,bar_record_id,price,status,equity,drawdown}`.
- `RISK_LATCHED`: `{kind,limit_type,observed,limit,trading_day}`.
- `CONTROL_REQUESTED`: `{kind,action,operator_intent,idempotency_key}`.
- `RECOVERY_STATE`: `{kind,from,to,cursor,reason,evidence_mode}`.
- `CORRECTION_OBSERVED`: `{kind,old_bar_record_id,new_bar_record_id,applied:boolean,
  reason:"BEFORE_CURSOR_APPLIED"|"LATE_AFTER_CURSOR"}`.
- `JOB_ATTEMPT_STATE`: `{kind,job_id,attempt_id,from,to,fencing_token,reason}`.
- `ARCHIVE_PUBLICATION_STATE`: `{kind,publication_id,from,to,parent_revision_id,
  dataset_revision_id|null}`.

Aggregate transition tables in sections 9-10 constrain `from/to`; an otherwise valid
payload with an illegal transition is rejected. New event variants require a schema
minor addition and readers retain unknown additive variants without applying them.
Changing the meaning or required fields of an existing variant requires a new major
schema and migration that preserves the old event bytes.

### 13.5 Invalid record examples

The following are invalid with `VALIDATION_ERROR`: a CompletedBar price not on tick;
a manifest ref without checksum; RunSubmitInput carrying `owner_user_id`; a market
order with a limit price; a close order with a bracket; a group with zero children;
an `add` between price and volume; `crosses_above` on a current literal; a rule root
that resolves to decimal; a StrategyVersion carrying an unknown module; a backtest
using `causal_latest`; or a forward lane using an unvalidated strategy. Cross-user
IDs instead fail `NOT_FOUND` before payload details are disclosed.

## 14. External gates and invalid assumptions

FT-03 must verify actual account eligibility, provider IDs, dated MGC metadata,
calendar, historical/live coverage, pacing, subscription, automated-use/retention
rights, login/recovery and any Level 2 pilot bounds. Until then, every contract price,
calendar and fee in the fixtures is synthetic. No subscription or ordinary display
entitlement proves archival rights. The Level 2 pilot is separate from this OHLCV
strategy contract.

The Netcup capacity, release budgets, backups and restore behaviour require measured
FT-16/FT-18 evidence. The paper model does not prove live fills, margin, liquidity,
queue position, broker isolation, unattended login or profitability. A missing bar
is not flat activity; a stale mark is not current equity; a successful reconstruction
is not contemporaneous paper operation.

## 15. Downstream contract index

The exact commissioning recipes and file seams are maintained in
`issue-recipes-v1.md`. That document should cite these stable headings rather than
copying semantics. Minimum consumers are: FT-01 section 1; FT-03 sections 3.2-3.4,
4 and 13; FT-04 sections 2, 3.1-3.2 and 11; FT-05 sections 3.4-3.5, 4 and 8; FT-06
sections 3.6 and 5; FT-07 sections 3.7-3.10 and 6-7; FT-08 section 9; FT-09 sections
4, 8-9; FT-10 sections 3.7-3.10 and 9; FT-11 sections 6-7 and 10; FT-12 sections
3.7-3.10 and 6.4; FT-13/FT-15 sections 2 and 11; FT-14 sections 3.8-3.10 and 6.4;
FT-16 sections 8, 11-13; FT-17 all fixtures and gates; FT-18 sections 9-10 and 12.

Unknown implementation symbols and validation commands must be filled from FT-01's
reviewed baseline before coding dispatch. The first issue eligible for commissioning
after both independent FT-02 reviews and identical-byte publication is FT-01. No
later issue should bypass that bootstrap dependency.
