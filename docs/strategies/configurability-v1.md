# Strategy configurability clarification

Status: user accepted this product direction on 2026-09-12. This clarification
supersedes language in the earlier Reversal–Breakout draft that could make a preset
value sound like a permanent engine restriction. It does not freeze all FT-02
contracts or authorise application implementation.

The original strategy packet and its review hashes remain unchanged as evidence of
that earlier draft. Read this clarification alongside it. Existing examples describe
the named preset; they are not universal expectations for every configuration.

## Configurable strategy behaviour

The app and MCP must use the same typed, versioned configuration. Changing supported
settings creates a new StrategyVersion; running lanes remain pinned. Users can
duplicate presets and compare independently replayed variants on the same owned
data without editing source or deploying code.

| Behaviour | Configuration requirement | Proposed initial preset |
| --- | --- | --- |
| Strategy family and side | Enable reversal/breakout and long/short/both | Reversal and breakout, both sides |
| Zones and indicators | Expose supported timeframe, lookback, pivot, zone width/touch/age and indicator parameters | Original-source parameter set |
| Filters | Enable/combine supported structure, volume and indicator conditions; specify arm or entry evaluation stage | Additional filters off |
| Breakout confirmation | Select supported beyond-level or strict-cross mode | Beyond-level |
| Retest and entry | Select supported entry policy; temporal conditions use explicit event order | Later completed-bar retest |
| Setup expiry | Bounded count of execution bars after arming | 24 bars |
| Entry-order lifetime | Separate bounded count of execution bars after submission; not coupled to setup expiry | 1 bar (15 minutes at the preset interval) |
| Exits | Select supported stop/target modes and their parameters | Original-source formulas under documented FamilyTrade timing |
| Sizing and risk | Expose supported sizing and configured limits in the appropriate strategy/run/account scope | Values still to be settled in FT-02 |

These defaults are recommendations, not proof of profitable or live-chart-equivalent
settings. FT-02 must specify each field's units, bounds, supported modes, transition
semantics and invalid combinations before coding. Existing one-bar order examples
remain preset examples; add a multi-bar order example and expiry/cancellation
boundaries when the order contract is frozen. Do not silently implement a permanent
one-bar constant. Setup and order lifetimes must remain distinct in the UI.

## Engine guarantees and current capability limits

Configuration cannot permit future-data access, fills before an order exists,
duplicate persisted effects, cross-user access or bypass of higher-level risk caps.
All enabled limits must be enforced. Historical and forward runs use the same saved
definition and declared execution model; unknown intrabar ordering stays explicit.

Configurable does not mean every conceivable behaviour ships initially. A new
indicator formula, zone algorithm or order model may need one reviewed extension;
its supported parameters then become configurable. Unsupported combinations must
be rejected clearly rather than silently approximated.

The first slice's one-position/one-entry-order-per-lane model is a documented
capability limit. Position stacking or multiple competing entry orders would require
additional accounting and lifecycle support. Likewise, supporting an earlier entry
policy cannot license a fill based on a retest that occurred before that order was
created. Safe ordering is permanent; the supported trading policies can grow.

## Next contract packet

Prepare the paper order/fill and risk specification: order activation/TTL,
gap and limit execution, stops and targets in the same candle, entry-bar exits,
commissions/slippage, sizing, risk enforcement and overnight/expiry treatment.
Recommend concrete defaults and numerical examples. Keep business choices visible;
do not ask the user to invent equations or choose internal library mechanisms.
