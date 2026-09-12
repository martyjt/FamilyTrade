# Planning decision record

Status: FT-02 recommended-default packet in final review; no implementation is authorised.

The continuation request authorises finishing FT-02 with recommended defaults,
worked examples and independent reviews. The concrete contracts in contracts-v1.md
and ft02-handoff-v1.md supersede pending-default entries in the historical table
below once both final-digest reviews PASS. Supported numerical trading choices
remain configurable; recommendations are paper-model assumptions, not provider
quotes, personal investment advice or permission to trade live. No new material
product choice is silently delegated to an implementation worker.

Current decisions are recorded precisely in contracts-v1.md: catalogue equations
and bounds, order/fill/risk defaults, overnight/expiry behaviour, recovery state
machines, small framework/auth choices and measurable routine-release budgets.
Provider/account/VPS evidence remains open. Read ft02-handoff-v1.md for the exact
freeze and commissioning gate. The table below is retained as the earlier record.

The user has offered to answer material product/trading questions before coding.
This record separates recommendations from accepted decisions. The planning specialist
updates the affected issue/contract after each answer; coding workers consume the
accepted result rather than reopening it.

| Decision | Accepted answer / pending options | Status |
| --- | --- | --- |
| Initial strategy editor | Bounded rule builder from the first release | Accepted 2026-09-12 |
| Holding period | Support overnight positions from the first release | Accepted 2026-09-12 |
| Broker proof | User still needs to open or fund an IBKR account | Recorded 2026-09-12; external dependency |
| Initial feature catalogue | One builder catalogue covering indicators, volume, market structure and support/resistance; exact initial primitives/formulas proposed in FT-02 | Awaiting precise scope/definitions |
| Pause with an open paper position | Both pause entries/manage exits and close-and-stop; persist user intent | Accepted 2026-09-12 |
| Paper-feed outage recovery | Automatic after verified repair/fresh data for previously running lanes; never override manual pause/stop | Accepted 2026-09-12 |
| Initial deployment interruption | Seconds-level routine application releases | Accepted 2026-09-12 |
| Numerical release budgets | Proposed <=5 seconds contiguous HTTP outage; MCP/worker gap budgets to settle in FT-02 | Awaiting agreement and later measurement |
| Later deployment continuity | Zero planned application-release downtime; host failure tolerance is separate | Accepted medium/long-term requirement |
| Initial strategy reference | Specify the Reversal–Breakout family from bentradingbot; preserve named source-default variants before testing additional filters | Planning authorised 2026-09-12; precise packet pending |
| Market-structure definitions | Assistant proposes objective definitions and research parameters; user need not supply a personal trading methodology | Accepted planning approach 2026-09-12 |
| Data subscription direction | Plan COMEX Level 2 per user and bounded depth collection early; candles remain the initial strategy input | Accepted planning direction 2026-09-12; account/permissions/pilot evidence still required |
| First strategy packet | Original/funded source-default inventory, proposed causal FamilyTrade baseline and 12 examples | Independently reviewed draft; user acceptance and wider FT-02 contracts pending |
| Configurability boundary | Trading choices are supported configurable settings; presets do not permanently lock strategy direction or entry lifetime; correctness and ownership guarantees remain enforced | Accepted 2026-09-12 |

The [configurability clarification](strategies/configurability-v1.md) takes precedence
over fixed-sounding preset language in the earlier draft. The original reviewed
packet remains unchanged for audit. This acceptance settles product configurability,
not every proposed numerical trading default. The next FT-02 packet is paper
order/fill and risk semantics, with recommended defaults and worked examples.

See the [strategy draft](strategies/reversal-breakout-v1.md) and
[review record](strategies/reversal-breakout-review-v1.md). The proposal preserves
source proximity/beyond-level setup rules; stricter crossing/approach requirements
are optional. Completed-data timing, frozen setup values, expiry/conflict rules and
enforced counters are explicitly different from Pine. Approving a draft direction
does not authorise implementation or establish performance parity.

The Level 2 direction does not authorise a purchase, account operation or live
collection. FT-03 must verify actual entitlement, automated use/retention rights,
depth reset behaviour and whether any historical depth backfill exists. A bounded
recorder pilot needs its own reviewed byte/time limits and operator authority;
it is independent of candle-strategy readiness. Keep physical archives separate per
user. A subscription is not proof of complete exchange order-by-order data.

Subsequent questions cover precise structure/level definitions, paper-fill assumptions,
risk defaults and release budgets. See [release continuity](release-continuity.md). Ask in small
batches and include the practical consequence of each choice. Do not ask users to
pick internal library methods, database syntax or other routine engineering details.

Accepted context already supplied: exchange-traded futures first; fewer than five
users; independent per-user subscriptions/storage; one-minute research bars; AG
Charts; Python-first small cohesive functional modules; Docker Compose on a Netcup
server being provisioned; configurable paper research and MCP; live orders later.

The earlier template-only scope is superseded by the accepted bounded rule builder;
templates may be examples/presets. Overnight support is required. No recommendation, time elapsed or
silence is an answer.
