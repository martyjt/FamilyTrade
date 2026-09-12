# FamilyTrade

A private futures research and trading application for fewer than five users.
Each user has independent broker credentials, market-data subscriptions, stored
data and trading records. Strategy lanes within that user's account reuse data.

## Status

Planning baseline only. A local untracked prototype was interrupted before testing
and is not an accepted implementation. No broker connection or deployment exists yet.

The current FT-02 [contract packet](docs/contracts-v1.md),
[worked examples](docs/contracts-examples-v1.json) and
[independent review record](docs/ft02-review-v1.md) lead to the
[first bounded commissioning packet](docs/bootstrap-packet-v1.md).
Read the [handoff](docs/ft02-handoff-v1.md) for the freeze gate and current readiness.
No implementation starts in this planning continuation.
Tradeferret remains a separate reference project; components will be reused only
where they fit this scope.

## Intended workflow

Connect a data source, collect one-minute futures candles, configure a strategy,
backtest it, forward-test it on paper, and compare its behaviour and results.
The web interface and authenticated MCP tools operate on the same versioned
strategy definitions and application operations.

Live trading is a later capability with explicit activation, risk limits and
broker reconciliation. It is not part of the first paper-trading slice.

## Start here

- [GitHub implementation plan](https://github.com/martyjt/FamilyTrade/issues/1)
- [First-slice milestone](https://github.com/martyjt/FamilyTrade/milestone/1)

- [Architecture and decisions](docs/architecture.md)
- [First slice and acceptance criteria](docs/first-slice.md)
- [Implementation backlog and dependencies](docs/implementation-plan.md)
- [Thin orchestration, workers and independent reviews](docs/session-workflow.md)

The initial implementation direction is Python, React/TypeScript with AG Charts,
PostgreSQL and per-user Parquet archives. Dependency versions and the broker/data
provider will be selected during the connection proof. No custom Rust component
is planned until profiling demonstrates a need.

Next planning task: [FT-02 contract specification](https://github.com/martyjt/FamilyTrade/issues/3). Application issues are not ready for dispatch until their contracts and dependencies are complete.
