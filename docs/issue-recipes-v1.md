# FT-02 downstream issue recipes v1

Planning packet, not implementation authority. `contracts-v1.md` is normative;
`contracts-examples-v1.json` supplies independent expected outcomes. The manifest
and independent review record identify the exact accepted content. Numbered
contract sections below are stable references. The original strategy draft is
consumed only with the contract's explicit overrides and configurability-v1.

## Common dispatch gate

FT-01 consumes the reviewed packet before Git bootstrap. All other issues wait for
their existing issue dependencies to be integrated. Before each dispatch the thin
coordinator records dependency SHAs, confirms the recipe's new paths against the
actual baseline, and supplies exact callable symbols from integrated dependencies.
Paths below are exact planned new-file seams, not claims that code already exists.
Do not dispatch with an unknown dependency symbol or unestablished test command.
That final baseline binding is routine evidence work, not permission to redesign.

Validation commands use the command interfaces established by FT-01: Python tests
through `uv run pytest <path>`, frontend tests through the FT-01 npm scripts, and
lint/type checks from `docs/development.md`. Every issue implements its owned tests
before claiming those commands pass. Pure contract fixtures are test inputs; do
not execute or adopt the untracked prototype to derive expected results.

For each relevant fixture, tests must assert the stated expected values/status,
ownership, event IDs and ordering, including failure outcomes. Partial successes
cannot close an external evidence criterion. Apply the issue's existing worker
profile and fresh reviewers A Terra/medium and B Sol/high; one mutating issue at a
time. No queue expansion, merge, deployment, account changes or live orders follows
from this document. All fixtures are synthetic unless explicitly evidenced otherwise.

## FT-01 — repository bootstrap (#2)

Consume contracts sections 1–3 (framework/publication/serialization conventions)
and the complete reviewed document manifest.
Use `bootstrap-packet-v1.md` for the individually audited prototype files, exact
allowed paths, empty-origin publication boundary, success/failure examples and
Windows/Linux clean-checkout commands. Do not implement the domain contracts.
Acceptance output: reproducible shell/locks/CI, preserved exact packet bytes and
one reviewed PR after an explicitly authorised documentation root. This is the
first bounded commissioning candidate once both packet reviews pass.

## FT-03 — provider evidence (#4)

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: access_two_user_denial, contract_expiry_close_and_no_roll, dst_session_mapping_uses_materialized_utc_calendar. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume sections 3–4, 8, 10 and 14. Create `docs/evidence/ft-03-provider.md` and, only
within commissioned proof scope, `tools/provider_probe.py` with no saved secrets.
Input: authorised per-user connection, actual dated contract and bounded request.
Output: redacted dated provider evidence for every FuturesContract field, calendar
revision, price/data mode, historical/live completion and correction behaviour,
pacing, independent entitlement/storage rights and supported VPS login/recovery.
Use the DST/expiry/ownership examples to explain expected mappings, but a synthetic
match is not provider proof. Validate probe bounds locally, then record exact
authorised calls and server measurements. Account/subscription/VPS availability
remain external gates. Depth collection needs its own separately bounded recorder
packet and authority; do not fold it into candle adapter implementation.

## FT-04 — access boundary (#5)

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: access_two_user_denial. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume sections 2–3, 11, 13. Owned new seams:
`src/familytrade/access/{models,service,repository,credentials}.py`, access migrations
under `migrations/versions/`, `tests/access/`. Input: authenticated session/token
and owned resource ID. Output: server-derived UserContext, owned BrokerAccount
view/redacted mutation result or the frozen denial envelope. Exercise two-user
denial, malicious owner fields, expired/revoked authentication, scopes, CSRF,
credential replace/revoke, encryption/key loss and no plaintext readback. Validate
`uv run pytest tests/access` with a disposable PostgreSQL database plus required
lint/types. Dependencies: FT-01; exact auth library/version comes from its locks.
No broker login automation, provider connection or public registration.

## FT-05 — archives and revisions (#6)

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: pinned_revision_survives_correction, dst_session_mapping_uses_materialized_utc_calendar, archive_publication_crash_after_rename, forward_active_corrections_before_and_after_cursor. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume sections 2–4, 8, 13. Owned seams:
`src/familytrade/market_data/{models,calendar,aggregate,archive,catalog}.py`, relevant
migrations, `tests/market_data/`. Input: owned completed-bar versions and bounded
read request. Output: immutable manifest/revision and precisely one selected
version of each bar. Implement manifest publication/recovery in the specified
order, not an assumed cross-filesystem/database atomic transaction. Exercise old
pinned revision after correction, active/archive overlap, every publication crash
point, tenant file denial, DST/session/partial-aggregation cases and retained-file
restore. Validate `uv run pytest tests/market_data` against disposable database and
filesystem. Calendar fixtures do not authorise live ingestion; FT-03 later supplies
actual metadata. No networking, cross-user deduplication or depth archive.

## FT-06 — strategy definitions (#7)

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: fully_serialized_rule_definition_crossover, invalid_rule_definition_has_typed_errors, strategy_edit_creates_version_and_running_lane_stays_pinned, entry_order_multi_bar_ttl_expiry, next_zone_targets_long_short_and_missing, r_multiple_targets_long_short, generic_market_entry_resolves_relative_bracket. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume sections 2–3, 5–7, 13; configurability-v1 and the Reversal–Breakout source
inventory under the authoritative override rules. Owned seams:
`src/familytrade/strategies/{definitions,validation,repository,presets}.py`, strategy
migrations, `tests/strategies/test_definitions.py`. Input: bounded typed rule tree
and supported configuration. Output: structured validation or immutable saved
StrategyVersion/hash. Validate `uv run pytest tests/strategies/test_definitions.py`.
Exercise all frozen invalid-type/bounds/future-reference cases; a combination
beyond presets; long/short/both; arm-vs-entry filters; one- and multi-bar order TTL
independently of setup expiry; pinned version after edit. Store vocabulary and
defaults once for both UI/MCP; no template-only narrowing or `eval`/user code.
Indicator evaluation and fills belong to FT-07, not this issue.

## FT-07 — pure engine (#8)

Dependencies include FT-05 for shared FuturesContract/CompletedBar/DatasetRevision
models and calendar/aggregation, as well as FT-06 definitions. FT-01 does not create
application schemas. Bind these integrated types before engine dispatch.

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: indicator_warmup_equality_and_causal_catalogue, confirmed_pivot_plateau_and_regime_availability, long_entry_then_stop_gap, short_entry_then_stop_gap, long_target_gap_price_improvement, short_target_gap_price_improvement, both_hit_after_open_fill_is_stop_first, intrabar_entry_with_stop_and_target_is_conservative_stop, fees_and_open_end_position_marked_not_liquidated, daily_loss_with_overnight_carried_position, entry_order_multi_bar_ttl_expiry, market_entry_gap_rejected_by_fill_time_risk, contract_expiry_close_and_no_roll, forward_delayed_bar_availability_and_exit_rule, exit_rule_unknown_does_not_close, next_zone_targets_long_short_and_missing, r_multiple_targets_long_short, favorable_limit_gaps_revalidate_bracket_both_sides, generic_market_entry_resolves_relative_bracket. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume sections 3–7, 10 (pure state and checkpoint payload only), 13 and both
example JSON files with their override notes. Owned seams:
`src/familytrade/strategies/{indicators,rules,zones,setups}.py`,
`src/familytrade/simulation/{state,orders,fills,risk,engine}.py`, `tests/engine/`.
Input: explicit pinned config/contract/calendar, prior immutable state and next
eligible completed event/control. Output: new state, decisions, intents, fills and
events; no I/O/implicit clock. Assert every numerical long/short gap, both-hit,
entry-bar ordering, limit/market/TTL, fees, warm-up/equality, risk, carry/expiry and
open-end outcome, including bar-close drawdown and data-quality labels. Compare
batch and incremental replay including duplicates at the caller boundary.
Validate `uv run pytest tests/engine`. No broker adapter or production checkpoints.

## FT-08 — jobs and schedules (#9)

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: job_owner_crash_and_stale_fence, schedule_occurrence_crash_overlap_misfire_and_dst. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume sections 2–3, 9, 11–13. Owned seams:
`src/familytrade/jobs/{models,repository,worker,schedules}.py`, queue migrations,
`tests/jobs/`. Input: owned bounded job/schedule/control with idempotency key.
Output: durable job/attempt states and fenced operation result. Test two concurrent
claimants, stale token and expired lease commits, heartbeat renewal, retry exhaustion,
cancel-vs-completion race, disabled schedule, coalesced missed runs, DST policy and
per-user/global limits. Assert every frozen crash/schedule timeline; unique durable
effects depend on fenced operation transactions, not delivery claims. Validate
`uv run pytest tests/jobs` with real disposable PostgreSQL concurrency tests.
No Codex scheduler, external broker or general orchestration service.

## FT-09 — ongoing data adapter (#10)

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: pinned_revision_survives_correction, dst_session_mapping_uses_materialized_utc_calendar, manual_pause_wins_over_automatic_recovery, forward_active_corrections_before_and_after_cursor. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume sections 3–4, 8–10, 14 plus FT-03's actual provider mapping/evidence.
Owned seams: `src/familytrade/market_data/{provider,ingestion,repair}.py`, ingestion
migrations, `tests/ingestion/`. Input: independently entitled feed plus durable
cursor. Output: owned completed/corrected revisions, coverage, freshness and feed
status. Validate `uv run pytest tests/ingestion`; inject duplicates, delayed data,
pacing, closed session and correction/reconnect cases, then perform the separately
authorised two-user integration run. Fenced feed ownership and actual observed
completion rules must agree with FT-03. Do not assume history can repair every gap.
No depth recorder, live orders or substitute provider without a product decision.

## FT-10 — owned backtests (#11)

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: same_user_two_lanes_are_accounting_independent, pinned_revision_survives_correction, fees_and_open_end_position_marked_not_liquidated, job_owner_crash_and_stale_fence. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume sections 2–3, 6–9, 11, 13. Owned seams:
`src/familytrade/research/{service,repository,worker}.py`, run migrations,
`tests/research/`. Input: frozen RunSpec and operation idempotency key. Output:
durable run/job IDs, status, paginated decisions/fills/results. Exercise revision
pinning after correction/edit, same-user independent lanes, cancel/retry checkpoint
boundaries, failed/incomplete results, denied exports and exact deterministic
reruns. Assert accounting fixtures without re-deriving expected values from engine
output. Validate `uv run pytest tests/research` with integrated archive/engine/jobs.
No unbounded search, chart UI, or prototype in-memory persistence adoption.

## FT-11 — persistent paper lanes (#12)

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: lane_crash_before_and_after_atomic_checkpoint, manual_pause_wins_over_automatic_recovery, daily_loss_with_overnight_carried_position, contract_expiry_close_and_no_roll, entry_order_multi_bar_ttl_expiry, forward_delayed_bar_availability_and_exit_rule, forward_active_corrections_before_and_after_cursor, worker_checkpoint_rollback_compatibility. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume sections 2–4, 6–11, 13. Owned seams:
`src/familytrade/paper/{service,repository,worker,recovery}.py`, lane migrations,
`tests/paper/`. Input: owned pinned lane, durable operator intent, eligible feed
events and fenced lease. Output: atomic cursor/state/decision/fill/event checkpoint
and visible runtime status. Test every lifecycle row with flat/open positions,
pause-manage, close-pending, stale feed, closed session, resume, version change,
overnight carry, correction before/after cursor, outage replay and restart. Include
both before-commit and after-commit crashes and an expired old owner after takeover.
No automatic recovery may reverse manual intent or label reconstructed fills as
contemporaneous. Validate `uv run pytest tests/paper` against integrated dependencies.
No broker account netting or automatic roll.

## FT-12 — metrics and comparisons (#13)

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: long_entry_then_stop_gap, short_entry_then_stop_gap, fees_and_open_end_position_marked_not_liquidated, zero_trade_metrics_are_defined. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume sections 3, 6–7, 10–11, 13. Owned seams:
`src/familytrade/analysis/{metrics,comparison,service}.py`, `tests/analysis/`.
Input: owned paginated immutable run/decision/fill/equity evidence. Output: defined
metrics, assumption differences and traceable trade reasons. Assert independent
cash/equity/fees/drawdown values, open end, losing short, zero trades, quality labels,
incomplete result and incompatible-currency comparison. Preserve all attempted
variants and explicit holdout dates. Validate `uv run pytest tests/analysis`.
Do not recalculate historical decision evidence using newer data or imply profit
is an acceptance threshold.

## FT-13 — browser authoring and runs (#14)

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: fully_serialized_rule_definition_crossover, invalid_rule_definition_has_typed_errors, strategy_edit_creates_version_and_running_lane_stays_pinned, access_two_user_denial, forward_delayed_bar_availability_and_exit_rule, exit_rule_unknown_does_not_close, generic_market_entry_resolves_relative_bracket. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume sections 2–3, 5–7, 11, 13. Owned seams:
`frontend/src/{api,auth,strategies,runs}/`, `frontend/tests/authoring.spec.ts`.
Use the shared validated API vocabulary; create no independent frontend formulas.
Browser input: login, editable supported tree/parameters and owned run request.
Output: immutable saved version, actionable validation, durable run status.
Test a non-preset rule combination, changed side and multi-bar TTL, distinct setup
expiry, stale form, session expiry, reload, invalid nested rule and second-user
crafted request. Run frontend lint/type/unit/build plus the Playwright command
established for this issue (`npm --prefix frontend run test:e2e -- authoring`).
No charting, general graph editor, live controls or paper-service implementation.

## FT-14 — charts and lane controls (#15)

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: same_user_two_lanes_are_accounting_independent, fees_and_open_end_position_marked_not_liquidated, manual_pause_wins_over_automatic_recovery, access_two_user_denial. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume sections 3–7, 10–11, 13. Owned seams: `frontend/src/{charts,paper,analysis}/`,
`frontend/tests/paper-analysis.spec.ts`. Input: bounded owned bars and recorded
events/metrics plus shared lane control calls. Output: chart ranges, truthful
signal/fill markers, equity/drawdown, comparison and durable manual intent/status.
Exercise two same-user lanes, multi-user denial, ambiguous entry/exit time ranges,
open-end position, corrected versus pinned data, stale/reconstructed labels,
pause-manage and offline close-pending through reconnect. Validate request bounds,
frontend checks and `npm --prefix frontend run test:e2e -- paper-analysis`; inspect
screenshots. Verify actual AG Charts entitlement separately; no private licence
text in packet/issue. No depth charts or claimed tick-level chronology.

## FT-15 — MCP adapter (#16)

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: access_two_user_denial, fully_serialized_rule_definition_crossover, invalid_rule_definition_has_typed_errors, manual_pause_wins_over_automatic_recovery, forward_delayed_bar_availability_and_exit_rule, generic_market_entry_resolves_relative_bracket. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume sections 2–3, 5, 9–11, 13–14. Owned seams:
`src/familytrade/mcp/{server,auth,tools}.py`, `tests/mcp/`, `docs/mcp-client.md`.
Map the contract's named operations/inputs/results directly to shared services;
handlers must not implement rules/fills. Input: authorised scoped request and
bounded arguments. Output: matching validation/error envelope, owned IDs and
paginated evidence. Verify protocol/version/transport compatibility using primary
docs at implementation, without weakening frozen auth or adding ad hoc bypasses.
Validate `uv run pytest tests/mcp`, malicious owner/token/audience/scope cases,
idempotent retries/reconnect, cancel and client disconnect with ongoing saved lane.
An actual supported Codex/Claude client round trip remains required evidence.
No live-order tool, arbitrary code, credential passthrough or always-on LLM.

## FT-16 — deployment and backups (#17)

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: archive_publication_crash_after_rename, lane_crash_before_and_after_atomic_checkpoint, manual_pause_wins_over_automatic_recovery. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume sections 8–12, 14 and FT-03's verified gateway/VPS constraints. Owned seams:
`compose.yaml`, deployment Docker/proxy config under `deploy/`,
`docs/deployment.md`, `docs/evidence/ft-16-restore.md`, `tests/deployment/`.
Input: commissioned host/settings, exact images and encrypted backup destination.
Output: persistent isolated mounts, authenticated HTTPS, bounded workers and
database/archive-consistent backup/restore evidence. Validate Compose/config and
`uv run pytest tests/deployment`, then authorised clean-host deployment, resource
measurement and restore of pinned revisions and paused/open lanes. Record measured
RPO/RTO, backup corruption/key-loss/disk-full failures. Local checks cannot close
host/provider criteria. No DNS changes, paid destination or deployment without
authority; release cutover tooling belongs to FT-18.

## FT-18 — release continuity (#19)

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: release_worker_handoff_with_open_overnight_lane, job_owner_crash_and_stale_fence, manual_pause_wins_over_automatic_recovery, worker_checkpoint_rollback_compatibility. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume sections 8–12, 14; FT-16 deployment baseline. Owned seams:
`deploy/release/`, `tests/release/`, `docs/evidence/ft-18-release.md`.
Input: compatible old/new images/schema/checkpoint versions and explicit release
class. Output: prewarmed cutover/drain or fail-before-switch, fenced worker handoff,
rollback and measured public-path budgets. Assert the contract's probe windows,
rates and numerical thresholds. Test failed readiness, forced stale owner, failed
post-switch health, rollback, open overnight and manual-pause cases, and web-only
release with untouched workers/feed. Validate `uv run pytest tests/release` plus
authorised VPS probe evidence. Host/database/gateway maintenance is separately
classified, never excluded after a failed ordinary release to manufacture a pass.

## FT-17 — first-slice acceptance (#18)

Exact fixture path: `docs/contracts-examples-v1.json`; case IDs: ALL. Assert their `expected` values, with the full semantics/crash tables above as additional boundary coverage.

Consume all contracts, fixtures and first-slice criteria; no additional product
implementation. Owned seams: `docs/evidence/ft-17-acceptance.md` and bounded
end-to-end acceptance tests under `tests/acceptance/` / `frontend/tests/acceptance/`.
Record exact integrated revisions and evidence for two independently entitled
users, one user's two lanes, one full intended session, UI and MCP configuration,
backtest and forward paper, overnight handling, interruptions, release and restore.
Exercise denied cross-interface/resource access and actual data-mode labels.
Validate focused end-to-end commands established by dependencies; record actual
operator/account/server dates and outcomes. Synthetic vectors prepare these checks
but cannot close them. Live orders stay disabled; unresolved external evidence
keeps acceptance open. FT-18 is a required dependency despite numeric issue order.
