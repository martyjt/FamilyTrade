# First slice: two-user futures research and paper trading

GitHub: https://github.com/martyjt/FamilyTrade/issues/1

## Outcome and scope

Build a private application for fewer than five users to collect independently
licensed/stored futures data, configure strategies, run reproducible backtests and
persistent forward-paper lanes, inspect AG Charts and use authenticated MCP.
Start with actual dated Micro Gold contracts and one-minute source candles. Two
lanes under one user reuse owned data but retain independent paper balances/state.

Keep the agreed architecture: small Python modules, React/TypeScript, PostgreSQL
operational state, per-user Parquet archives and Docker Compose on one Netcup VPS.
Pure strategy/simulation functions have explicit inputs/state; UI/MCP share
application operations. No custom Rust, general workflow engine, cross-user data
deduplication, always-on LLM, live orders or automatic profit-based activation.

## Current contract and readiness

FT-02 #3 carries the complete contracts-v1 packet, worked examples, per-issue
recipes, exact manifest and independent review evidence. `ft02-handoff-v1.md`
defines the gate: both final-digest reviewers PASS, findings resolved, and exact
GitHub artifact readback verified. Before this gate the packet is in review.
After it, FT-02 is PLANNING_COMPLETE and FT-01 is READY_FOR_COMMISSIONING. This
planning continuation starts no implementation.

Accepted scope remains a bounded composable rule builder, configurable strategy
settings, overnight positions, separate setup and entry-order lifetimes, pause
entries/manage exits, close-and-stop, persistent manual intent, verified automatic
outage recovery and seconds-level routine releases. Recommended numerical defaults
are paper-model assumptions, saved with each run/version, not provider fee quotes
or profitable-trading claims. The reviewed contract supersedes prior pending-default
statements and draft restrictions. Original Reversal–Breakout source fingerprints
and its draft reviews remain preserved as provenance, under explicit overrides.

The first bounded implementation issue is FT-01 #2, using
`docs/bootstrap-packet-v1.md`. The remote has no initial commit at planning time;
that packet specifies the documentation-root publication boundary and individual
prototype-file exclusions. Do not stage the untracked prototype wholesale.

## Thin coordinator, worker and reviewers

A separately commissioned coordinator uses `docs/session-workflow.md`: Sol/medium
coordinator, each issue's assigned worker, independent Reviewer A Terra/medium and
Reviewer B Sol/high. One mutating issue/worktree at a time; fresh reviewer contexts
inspect the same final candidate. Changed content requires final reviews again.
The coordinator verifies integrated dependency SHAs, exact baseline file/symbol
bindings, fixtures, commands and authority before dispatch. It does not implement
application code or start a successor outside the commissioned queue.

FT-01's empty-origin documentation root and subsequent worker commit/push/PR need
explicit commissioning authority. Merge, deployment, purchases, account operations,
provider contact and live orders remain separate. An open issue or existing PR is
not an integrated dependency or permission to act.

## External evidence

IBKR remains a candidate; the user still needs to open or fund an account. FT-03
must prove account/API eligibility, independent subscriptions, automated-use and
retention rights, dated contract/calendar facts, real-time/delayed mode, pacing and
supported local/VPS login/recovery. Missing provider evidence stays open. COMEX
Level 2 per user and bounded early collection remain a planning direction; the
recorder pilot needs its own reviewed limits and authority and does not block
candle strategy contracts. No purchase or live collection is authorised here.

Actual Netcup capacity, AG Charts entitlement, supported MCP client round trips,
full-session paper operation, backup restoration and release continuity require
observed evidence in their issues. Synthetic examples never close these criteria.
Host-failure tolerance and zero planned release downtime remain later work.

## Child issues

- [ ] https://github.com/martyjt/FamilyTrade/issues/2 — FT-01 bounded repository bootstrap; first commissioning candidate
- https://github.com/martyjt/FamilyTrade/issues/3 — FT-02 reviewed contracts and examples; completion is the linked issue's verified freeze/publication state
- [ ] https://github.com/martyjt/FamilyTrade/issues/4 — FT-03 provider/account/VPS proof
- [ ] https://github.com/martyjt/FamilyTrade/issues/5 — FT-04 identity and owned credentials
- [ ] https://github.com/martyjt/FamilyTrade/issues/6 — FT-05 archives and revisions
- [ ] https://github.com/martyjt/FamilyTrade/issues/7 — FT-06 validated strategy definitions
- [ ] https://github.com/martyjt/FamilyTrade/issues/8 — FT-07 pure decisions and fills
- [ ] https://github.com/martyjt/FamilyTrade/issues/9 — FT-08 durable jobs and schedules
- [ ] https://github.com/martyjt/FamilyTrade/issues/10 — FT-09 ongoing ingestion and repair
- [ ] https://github.com/martyjt/FamilyTrade/issues/11 — FT-10 owned backtest jobs
- [ ] https://github.com/martyjt/FamilyTrade/issues/12 — FT-11 persistent paper lanes
- [ ] https://github.com/martyjt/FamilyTrade/issues/13 — FT-12 metrics and evidence
- [ ] https://github.com/martyjt/FamilyTrade/issues/14 — FT-13 browser authoring and runs
- [ ] https://github.com/martyjt/FamilyTrade/issues/15 — FT-14 charts and paper controls
- [ ] https://github.com/martyjt/FamilyTrade/issues/16 — FT-15 authenticated MCP
- [ ] https://github.com/martyjt/FamilyTrade/issues/17 — FT-16 deployment and restore
- [ ] https://github.com/martyjt/FamilyTrade/issues/18 — FT-17 complete two-user acceptance; depends on FT-18
- [ ] https://github.com/martyjt/FamilyTrade/issues/19 — FT-18 measured releases and rollback

The list preserves issue order, not a commissioned mutable queue. Dependency links
in each child and `docs/implementation-plan.md` remain authoritative. No child
closes until its own acceptance and external evidence are satisfied.

## Milestone acceptance

All child criteria must link to exact integrated revisions and reproducible evidence.
Two independently entitled users and one user's two isolated lanes complete the
actual data/app/paper/MCP workflow, including session/overnight boundaries, controls,
recovery, release and restore. Simulation limitations remain visible. Live orders
stay disabled. FT-02 completion alone does not close this parent or any code issue.
