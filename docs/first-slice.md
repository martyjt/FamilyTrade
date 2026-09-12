# First slice and acceptance criteria

The reviewed [contracts-v1](contracts-v1.md) and [issue recipes](issue-recipes-v1.md)
make the criteria below concrete. Their freeze requires both final-digest reviews
and publication; see [the handoff](ft02-handoff-v1.md). Synthetic examples never
replace the account, provider, user, client or VPS evidence required for this slice.

## Objective

Demonstrate one user collecting Micro Gold one-minute data and running two
independent, configurable paper strategies, then verify isolation with a second
user. Establish reliable data and repeatable behaviour before expanding features.

## Sequence

1. Freeze reviewed module/domain contracts in planning, then establish the repository baseline.
2. Prove the candidate broker/data connection locally and on a representative VPS;
   develop independent foundation components against labelled fixtures while waiting.
3. Connect owned archives, durable jobs, strategy definitions and the paper engine.
4. Add strategy forms, charts, comparison views and authenticated MCP tools.
5. Verify two users' independent connections, storage and results on the deployed slice.

The [implementation plan](implementation-plan.md) defines bounded issues and exact
dependencies. V1 includes a bounded indicator/condition rule builder and supports overnight positions.
The reviewed contract defines its allowed rules, exits, sessions and risk behaviour. It does not execute
arbitrary user code. Follow the [session workflow](session-workflow.md); a thin coordinator dispatches issue workers and independent reviewers only after planning readiness.

Do not copy Tradeferret wholesale. Inspect individual components for relevance and
tests when an implementation need arises. Keep the old checkout unchanged.

## Connection proof

- Confirm approved API access and permissions for collection, retention and use.
- Resolve a specific Micro Gold expiry and verify multiplier/tick/session metadata.
- Request a bounded historical sample and receive ongoing data, recording whether
  the source is real-time or delayed. Do not silently substitute delayed data.
- Measure request limits, data completeness and actual gateway resource use.
- Demonstrate supported login, reconnect and restart recovery on the proposed VPS;
  document operator steps and any reauthentication requirement.
- Repeat with independent user sessions before claiming multi-user readiness.

An unavailable account or unconfirmed provider permission is an explicit external
dependency. Synthetic fixtures may support development but do not pass this proof.

## Functional acceptance

| Area | Evidence required |
| --- | --- |
| Collection | Historical backfill and ongoing completed candles appear with source, user and contract identity |
| Scheduling | A scheduled repair resumes from saved progress and prevents overlapping duplicate work |
| Quality | Deliberately missing or stale input is visibly flagged; repeated ingestion does not duplicate candles |
| Strategy editing | Changing a supported rule/parameter in the app creates a validated new version without source edits or deployment |
| Lanes | Two paper lanes reuse one user's data while their cash, positions, fills and results remain independent |
| Historical replay | A bounded backtest and equivalent event replay produce the same decisions under identical inputs |
| Charts | AG Charts shows candles, signals and simulated fills for the selected user, contract and lane |
| Analysis | Run comparison includes net costs, drawdown, trade count and recorded reasons for individual decisions |
| MCP | An authenticated client creates a draft, submits a bounded backtest and retrieves/compares results through the same operations as the UI |
| Isolation | A second user cannot read, alter or reference the first user's data, credentials, jobs, files or results through UI/API/MCP |
| Recovery | A worker restart preserves completed work and does not duplicate strategy decisions or simulated fills |
| Controls | Pause entries/manage exits and close-and-stop preserve manual intent across recovery; offline closure remains pending |
| Release continuity | FT-18 proves agreed seconds-level app cutover/rollback with separate HTTP/MCP and lane/ingestion evidence |
| Capacity | Record memory/CPU, ingestion lag, run duration and disk growth with research running alongside ingestion |

## Simulation rules

Document execution timing: a decision based on a completed candle cannot receive a
fill earlier in that candle. Include commissions, tick rounding and configurable
slippage. If a candle crosses both stop and target, apply an explicit conservative
policy and flag the ambiguity; do not claim the actual event order is known.

Record whether fills use trade bars, quotes or other inputs. Include contract rolls
and session boundaries in validation. Passing synthetic examples does not establish
market profitability or prove broker execution behaviour.

## Validation approach

Use focused tests for strategy determinism, fill timing, data deduplication, tenant
isolation and crash/recovery boundaries. Exercise the provider adapter with a small
authorised integration run. Verify user workflows in the browser and MCP client.
Capture limitations and measured results beside the implementation when available.

## Deferred

- Live order submission and its risk/reconciliation acceptance gate.
- Multiple live strategies netting into the same account/contract position.
- Order-book strategies, tick archives and high-frequency execution.
- Arbitrary user code, visual workflow graphs and AI councils.
- Custom Rust extensions, distributed infrastructure and broad provider coverage.
- Zero planned application-release downtime after initial seconds-level continuity;
  host-failure resilience is a separate future scope decision.

The first slice is accepted on reliability, isolation and reproducibility, not
paper profitability. Live trading requires a separate explicit readiness review.
