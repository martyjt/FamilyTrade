# Reversal–Breakout v1 — DRAFT contract

Status: **reviewable proposal; not accepted or frozen**. This packet specifies one
bounded FamilyTrade preset and a breakout-only variant. It does not claim parity
with TradingView, reproduce a live chart, establish profitability, or make the
whole FT-02 contract ready. Fill/accounting, calendars, contract-master data and
lane persistence remain governed by the eventual reviewed FT-02 contract.

## Provenance and observed source behaviour

Read-only source snapshot: `D:\Projects\bentradingbot`, clean commit
`8cd1cbc6d8c2306e7c3c731d159c98b255efda31`.

| Source | SHA-256 | Relevant lines |
| --- | --- | --- |
| `reversal_breakout.pine` | `c6cf2d46e54f65aa4e9d35e888af5a7a1cd611876b9840a57732c6fd91ddcbc7` | defaults 47–123; pivots/zones 157–274; reversal 279–339; breakout 348–408 |
| `reversal_breakout_funded_v2.pine` | `393dcb69db493e5e3d11c85fe2946610b87ac89a1d75901b31f609d4d433ab0b` | executable defaults 77–145 and 299–330; pivots/zones 365–480; entries 547–702; alert bridge 707–782 |

The funded-v2 header repeats the original parameter list at lines 71–74, but its
executable inputs differ. The table below follows executable inputs, not prose or
unknown saved chart settings. Neither column is evidence of the live configuration.

| Parameter | Original source default | Funded-v2 source default | Proposed MGC baseline |
| --- | ---: | ---: | ---: |
| execution interval (external to Pine inputs) | header says 15m validated | header says 15m | 15m |
| zone interval | 5m | 5m | 5m |
| ATR length / ATR units | 20 / on | 10 / on | 20 / on |
| pivot left / right | 3 / 3 | 3 / 3 | 3 / 3 |
| merge distance / maximum width | 0.25 / 0.50 units | same | same |
| minimum touches / retained zones / age | 1 / 12 / 500 | 1 / 29 / 500 | 1 / 12 / 500 zone bars |
| zone cooldown | 0 | 1 execution bar | 0 |
| reversal / approach / stop buffer | on / 1.50 / 0.40 | on / 2.50 / 1.00 | original |
| recent-peak stop / lookback | on / 12 execution bars | same | same |
| breakout / break / pullback tolerance | on / 1.00 / 0.50 | same | same |
| breakout expiry / stop buffer | 24 / 0.80 | 18 / 1.00 | 24 / 0.80 |
| VWAP stop | on | on | on |
| target / measured move / R multiple | measured / 1.0 / 3.0 | same | same |
| RTH only / maximum entries per trading day | off / 23 | off / 200 | off / 23 |
| quality gate | absent | off (`RR floor` and random control optional) | off |
| null displacement test | off, 2 ATR, seed 1 | same | off; research-only |
| recent-bars restriction | absent | 3,000 | none; dataset bounds are explicit |
| automation | off, symbol MGC | on, symbol MCL | excluded from strategy definition |
| require directional approach | unavailable | unavailable | off; optional variant |
| breakout crossing mode | merely beyond threshold | merely beyond threshold | `beyond`; optional `strict_cross` |

Both sources calculate `unit = zone ATR` when ATR is enabled, otherwise one price
point. They derive merge, width, approach, break, pullback and stop distances by
multiplying that unit. Reversal long uses entry at support top, stop at the lower
of `support low - buffer` and `recent low - buffer`, and target at resistance low;
short is symmetric. Breakout long records support top as the edge, impulse
`close - edge`, enters at `edge + pullback tolerance`, stops at the lower of
`far edge - buffer` and `VWAP - buffer`, and targets `edge + impulse × multiple`.
Short is symmetric. “Next zone” and R-multiple targets are supported source modes.

## Proposed FamilyTrade preset

`reversal_breakout_mgc_original_v1` uses the proposed-baseline column above with
both reversal and breakout enabled. `breakout_mgc_original_v1` is identical except
`reversal.enabled=false`. A separate read-only reference preset records the funded-v2
input defaults; it is not the MGC recommendation.

Every run names an actual Micro Gold futures expiry, for example root `MGC`, expiry
`2026-12`, and a provider-resolved contract ID. Root-only `MGC`, a continuous
symbol, or an unverified example ID is invalid for execution. The example expiry is
illustrative; FT-03 must supply the authoritative provider ID, last-trade timestamp,
tick size, multiplier and exchange calendar for the run date.

The strategy is a typed, versioned stateful block in the same bounded definition
accepted by UI and MCP. Required blocks are `confirmed_pivot_zones_v1`,
`reversal_setup_v1`, `breakout_retest_v1`, `one_position_v1` and `bracket_exit_v1`.
Parameters are scalar, enum or bounded condition-group fields. There is no script,
expression text, webhook secret, broker bridge, or arbitrary code field.

Optional feature gates are independent and default off:

- `structure_filter_v1`: a separately versioned causal regime primitive.
- `relative_volume_filter_v1`: completed-bar volume only, with explicit lookback.
- `adx_filter_v1`: separately frozen Wilder equations and warm-up.

Turning on one gate creates a new StrategyVersion and must not change the raw
zone/setup formulas. Level 2/order-book data is a separate future pilot and is not
an execution prerequisite. The first slice needs only each user’s independently
licensed one-minute OHLCV, aggregated into complete 5-minute zone bars and complete
15-minute execution bars.

Each enabled breakout filter declares `evaluation_stage: arm | entry_intent`, default
`entry_intent`; reversal filters allow only `entry_intent`. `arm` supports a
volume-on-break experiment. Evaluate arm-stage filters after all raw arm candidates
are derived and before state mutation. `FAIL` or `UNKNOWN` records the reason,
discards that candidate and prohibits its same-bar re-arm; if none survives, the
slot stays `IDLE`. Apply the opposing-arm rule to survivors.
Evaluate entry-intent filters on the completed retest/reversal bar after prices are
frozen and before conflict resolution. Rejection cancels that arm/candidate with a
reason and prohibits same-bar re-arming. Every gate must return `PASS`; warm-up or
missing input is `UNKNOWN`, never an older-value fallback. Each result records the
feature version, value, source bar ends and `known_at` decision time; arm evidence
remains frozen with the arm. A filter cannot be enabled until FT-02 freezes its
causal formula, inputs and warm-up. Disabling all filters restores the raw path.

The shared validator accepts finite decimal multipliers only. Recommended initial
bounds are: ATR length `2..500`; pivot left/right `1..50`; merge, width, approach,
break, pullback and stop multipliers `(0,100]`; recent-peak lookback `1..500`;
minimum touches `1..100000`; `max_zones 2..200`; zone age `1..100000` zone
bars; cooldown `0..1000` execution bars; breakout expiry `1..1000`; measured-move
and R multiples `(0,100]`; and daily entries `1..1000`. The target mode is exactly
`measured_move`, `next_zone` or `r_multiple`. The first implementation accepts
execution intervals `{5m,15m,30m,60m}` and zone intervals `{1m,5m,15m}` only when
the zone interval is no longer than and divides the execution interval. The MGC
presets pin `15m/5m`; editing any value creates a new version rather than silently
changing a saved preset. Quantity and account-risk limits belong to RunSpec/lane
configuration, with one contract used by the examples.

## Causal FamilyTrade semantics

These are recommended material corrections, deliberately distinct from faithful
Pine output. TradingView documents that `request.security()` on a lower timeframe
returns one intrabar per chart bar; FamilyTrade processes every completed 5-minute
bar in event order. TradingView also warns that `calc_on_order_fills` can expose
historical OHLC values unavailable at the equivalent live instant. See
[Other timeframes and data](https://www.tradingview.com/pine-script-docs/concepts/other-timeframes-and-data/)
and [Strategies](https://www.tradingview.com/pine-script-docs/concepts/strategies/).
Consequently this contract promises reproducible FamilyTrade behaviour, not Pine
trade-list parity.

| Observed Pine behaviour | Proposed FamilyTrade behaviour |
| --- | --- |
| lower-timeframe `request.security()` samples one intrabar per chart bar | consume every completed zone bar in timestamp order |
| repeated equal pivot price can be suppressed by `lastPH`/`lastPL` | every time-distinct confirmed pivot is a touch |
| zone age compares chart `bar_index` although the label says zone bars | age in completed zone bars |
| reversal needs proximity, not movement toward the zone | preserve proximity in the base; offer directional approach as an opt-in gate |
| breakout needs only a close already beyond the threshold | preserve `beyond` in the base; offer `strict_cross` as an opt-in mode |
| the break candle’s high/low can also satisfy the pullback | require a later completed execution bar |
| ATR-on falls back to unit `1.0` when ATR is missing/nonpositive | reject that decision as `NOT_READY_ATR`; ATR-off uses unit `1.0` without ATR |
| ATR/VWAP and some target context can change while setup state survives | freeze all derived setup prices on arm/signal |
| zone cooldown is consumed when an entry order is placed | consume cooldown only on an atomic entry fill |
| funded-v2 documents a `var` rollback that can defeat its daily cap | enforce a fill-counted daily cap in persisted lane state |
| opposing arm blocks mutate sequentially, so short can overwrite long | derive from one pre-state and emit `CONFLICT_OPPOSING_ARMS` with no arm |
| several tagged limit entries can remain and re-enter within one bar | one lane entry order, next-event eligibility and deterministic conflict resolution |
| setup expiry does not cancel an already placed Pine order | every order has owned state, TTL and explicit cancellation |

All bars are `[start, end)` and usable only after final completion. Within one
15-minute decision boundary, process newly completed 5-minute bars oldest first,
then evaluate the 15-minute decision. A corrected bar creates a new dataset revision;
a completed run remains pinned to its old revision.

### Pivots and zones

For zone-bar index `k`, a pivot high with `(L,R)` exists when `high[k]` is greater
than or equal to each of the previous `L` highs and strictly greater than each of
the next `R` highs. A pivot low uses less-than-or-equal on the left and strict
less-than on the right. This chooses the last bar of an equal-price plateau. It is
confirmed and emitted only when bar `k+R` completes. Identity is
`(contract_id, zone_interval, kind, pivot_bar_end, confirmation_bar_end)`; equal
prices at different times are distinct touches. If a bar confirms both kinds,
process high before low.

True range is `max(high-low, abs(high-previous_close), abs(low-previous_close))`,
using `high-low` for the first bar. ATR requires `atr_length` consecutive valid zone
bars, seeds with their arithmetic mean true range, then updates as
`(previous_ATR*(length-1)+TR)/length`. A missing/invalid interval breaks warm-up.
For each pivot event in ATR-on mode, use ATR from its completed confirmation zone
bar; missing/nonpositive ATR means no pivot event is applied. ATR-off uses unit
`1.0` and applies the pivot without reading ATR. Search zones in ascending
creation sequence. Merge into the first zone for which the pivot is within
`[low-merge_distance, high+merge_distance]` and the resulting width is at most
`max_width`, using this event’s ATR unit. Otherwise create a point zone. A merge
changes bounds, touch count and `last_touch_zone_index`; it does not change zone ID
or creation sequence. This preserves deterministic bounded growth while fixing the
source’s price-based duplicate suppression.

Expire a zone before processing the next pivot/decision when
`current_completed_zone_index - last_touch_zone_index > zone_max_age`. After event
processing, retain the newest `max_zones` by creation sequence. A setup already
armed from a zone keeps its frozen snapshot even if that zone later merges or ages
out. A zone is setup-eligible only at `touch_count >= minimum_touches`. Among
eligible zones strictly above/below the decision close, select smallest price
distance; equal distance selects the newer zone, then lexical zone ID. Cooldown is
measured from the zone’s last atomic entry fill: a zone is eligible when
`current_execution_index - last_filled_index > cooldown`; an unfilled order does not
consume cooldown.

### Decision and setup state

At execution-bar end `T`, select the latest completed zone bar whose end is `<= T`.
ATR-on uses that bar’s ATR; missing/nonpositive ATR rejects setup evaluation as
`NOT_READY_ATR`, without searching backward or using the Pine fallback. A later zone
bar is unavailable. ATR-off sets unit exactly `1.0` and requires no ATR. Take one
immutable snapshot of selected
zones, unit, recent extreme, session VWAP and every derived entry/stop/target. Later
ATR, VWAP or zone changes never move an armed setup or resting order.

With `recent_peak_stop=true`, long uses the minimum low and short the maximum high
over exactly the latest `peak_lookback` completed execution bars, including the
signal bar. Require a full contiguous valid window since any missing/invalid bar;
otherwise reject as `NOT_READY_PEAK_WINDOW`. Freeze the extreme and derived stop on
the signal bar. With the option off, the peak window is neither read nor required.

A base reversal candidate requires the close to remain outside the relevant zone
and be within `approach_distance`, matching the Pine proximity test. A valid
opposite zone and correctly ordered stop/entry/target are required. The optional
`require_directional_approach=true` variant additionally requires distance to the
edge to be smaller than on the previous execution close. It defaults off because
that is a strategy change, not a causality requirement.

Reversal has no long-lived armed state: `IDLE → ENTRY_PENDING → POSITION_OPEN →
IDLE`, with `EXPIRED`/`CANCELLED` records for its order. It is freshly evaluated on
each completed execution bar.

Breakout state is `IDLE → ARMED → ENTRY_PENDING → POSITION_OPEN → IDLE`, with
`EXPIRED`/`CANCELLED` records. Base mode `beyond` matches the source: long arms when
the current close is strictly above `edge + break_distance`; short arms when it is
strictly below `edge - break_distance`, even if the crossing happened earlier. The
optional `strict_cross` mode also requires the previous close to be at or inside
the threshold. It defaults off because it changes which setups exist. Arm freezes
edge, far edge, impulse, unit, VWAP, entry, stop and target. A retest is eligible
only on a later execution bar: long when low is at or
below frozen `edge + pullback_tolerance`, short when high is at or above frozen
`edge - pullback_tolerance`. The arm remains eligible through exactly
`breakout_expiry` later bars and expires before bar `arm_index + expiry + 1` is
evaluated. A break and pullback in the same candle never enters.

An armed breakout owns the breakout slot until retest, expiry or cancellation;
later same-side or opposite-side break signals do not replace it. After expiration,
base `beyond` mode may create a new arm starting on the next execution bar if price
is still beyond a then-selected zone; `strict_cross` must observe a new crossing.
While an entry order is pending, evaluate no new entry candidates. Any entry fill
cancels every other armed setup before the position-open decision is committed.

Before creating an arm, derive every eligible long and short arm candidate from the
same pre-decision state. If both sides exist, atomically create neither and record
`CONFLICT_OPPOSING_ARMS` with both candidate snapshots. In the Pine block order a
long arm can be assigned and then overwritten by the short block; FamilyTrade does
not carry that ordering accident forward.

“Signal bar” means the completed reversal candidate bar or completed breakout retest
bar, never the earlier breakout arm bar. Immediately after that signal bar closes,
submit the frozen entry intent and persist `ENTRY_PENDING`. The proposal makes it
active only during the following execution-bar interval; when that following bar
completes, FT-02’s eventual gap/touch/slippage policy resolves its OHLC evidence.
The order was not created retroactively at the following bar’s close. If unresolved
as filled, cancel it at that bar end. No same-signal-bar or historical intrabar fill
is allowed. This one-bar TTL is proposed, not a substitute for FT-02’s fill table.

Each lane may have at most one position and one entry order. First discard invalid
candidates. Opposing long/short candidates on one decision produce no order and a
`CONFLICT_OPPOSING_SIDES` decision. Cancel every participating breakout arm, discard
participating reversal candidates, and prohibit same-bar re-arming. For same-side
reversal and breakout candidates, breakout wins because it completes a previously
armed sequence; transition it to `ENTRY_PENDING` and record the discarded reversal.
Same-family ties choose the smaller entry distance in ATR units, then
newer source zone, then lexical setup ID. This replaces Pine’s ability to leave
several independently tagged orders resting.

When an entry fills, cancel any other entry intent and create one position-attached
OCO stop/target bracket from frozen prices. A filled exit cancels its sibling.
Cancellation, order expiry, close-and-stop, contract expiry and end-of-run handling
must use FT-02’s reviewed event/fill rules. Strategy setup expiry alone never leaves
an untracked broker or paper order.

Positions may carry overnight. Exchange trading-day rollover resets only the daily
entry counter and session VWAP; it neither flattens a position nor rolls a contract.
An entry is allowed only while `filled_entries_in_trading_day < max_entries`; increment
the counter exactly once on the atomic entry fill, not on signal or order placement.
Entry-window closure blocks new entries and cancels every still-unfilled entry
order, whether already eligible or not; protective exits continue.
`pause_new_entries` persists, cancels
pending entries and armed setups, keeps zones/indicators warm and manages exits.
Resume waits for a fresh completed decision bar. `close_and_stop` also cancels entry
state and submits a close intent under FT-02; it remains pending when the market/feed
is ineligible. Automatic recovery may resume only a previously running lane after
freshness and consistency checks, and never overrides manual pause/stop.

## Remaining FT-02 dependencies and decisions

The recommendations requiring user acceptance are the causal/execution deltas above:
process all 5-minute bars, explicit plateau ties, later-bar retests, frozen setup
values, filter stages/rejection, atomic opposing-arm rejection, breakout-first
same-side priority, opposing-entry rejection, ATR-on rejection instead of fallback,
fill-counted zone cooldown, an enforced fill-counted daily entry cap, and
one-following-bar entry TTL. The base deliberately retains Pine’s proximity and merely-beyond tests;
directional approach and strict crossing are named opt-in variants, default off.

FT-02 still must freeze the exchange calendar/trading-day authority; exact ATR and
VWAP warm-up/reset fixtures; tick rounding by order side; gap, touch, both-hit and
entry-bar exit rules; commissions/slippage; contract-expiry liquidation; end-of-run
marking; event IDs/checkpoints; and correction/recovery behaviour. FT-03 must verify
the MGC contract metadata and each user’s data rights. Until those are resolved and
this exact packet is independently reviewed, it remains DRAFT and no downstream
issue is declared ready.

Golden hand calculations are in `reversal-breakout-examples-v1.json`. Their prices
use a synthetic MGC-like tick `0.10` and multiplier `10 USD/point`; they test signal
math and causality, not provider data or final fill/accounting policy.
