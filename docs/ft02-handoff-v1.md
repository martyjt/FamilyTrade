# FT-02 planning handoff v1

## Requested outcome and authority

On 2026-09-12 the user commissioned completion of FT-02 with recommended defaults,
worked examples and independent reviews while preserving configurable strategies.
Implementation stays paused. Only material unresolved product/trading choices go
back to the user; routine engineering choices and configurable paper-model defaults
are resolved in this packet. This is not live-trading, account, purchase, deployment,
commit/push, merge or implementation-worker authority.

## Authoritative packet and precedence

Read `contracts-v1.md`, `contracts-examples-v1.json`, `issue-recipes-v1.md` and
`bootstrap-packet-v1.md`. `planning-manifest-v1.json` identifies the exact reviewed
file bytes; `ft02-review-v1.md` records independent review results for its digest.
The freeze becomes effective only when both reviewers report PASS for that same
digest, all material findings are resolved, and the issue publication is verified.
Until then this is a review candidate, not a frozen contract.

The original `strategies/reversal-breakout-v1.md`, examples and review remain
historical evidence. Its draft-status text and unresolved FT-02 placeholders do
not override the final contracts. `strategies/configurability-v1.md` remains the
accepted product boundary. Source-default inventories establish provenance, not
live chart settings or profitable parameters. Contract definitions plus explicit
override rules supply the current implementation meaning.

Earlier statements in planning documents/issue history that precise defaults or
contracts remain to be written describe the previous planning state. This handoff,
the final reviewed contracts and each issue's recipe supersede them. No accepted
scope is removed: bounded rule builder, overnight carry, both paper controls,
verified automatic recovery, independent per-user data and seconds-level releases
remain required. New parameters always create immutable strategy versions; saved
lanes and runs remain pinned.

## Readiness after the final review gate

| Issue | State after both PASS reviews and publication | Next gate |
| --- | --- | --- |
| FT-02 / #3 | PLANNING_COMPLETE | No application code delivered |
| FT-01 / #2 | READY_FOR_COMMISSIONING | User commissions only the bounded bootstrap and its delivery authority |
| FT-03 / #4 | WAIT_DEPENDENCIES_AND_EXTERNAL_EVIDENCE | FT-01, actual account/data rights and VPS proof |
| FT-04 through FT-18 | WAIT_DEPENDENCIES | Integrated prerequisites and baseline symbol/command binding; external criteria where specified |

An issue being ready to commission does not start implementation. The first issue
is FT-01, not a strategy engine or provider adapter. Its exact recipe is
`bootstrap-packet-v1.md`; one thin coordinator uses one worker and two independent
reviewers under `session-workflow.md`. No successor queue is commissioned here.

## Durable publication before an initial Git commit

The GitHub FT-02 issue carries this handoff, reviewed specification/example
artifacts, manifest and review evidence before Git bootstrap. When a body would
exceed GitHub's limit, use clearly labelled issue artifact comments with exact
file content and links in the issue body; do not substitute a local-only path or
chat summary. Downstream issue bodies contain their exact recipe and refer to the
same contract digest/artifact links. Verify bodies and content against the local
reviewed bytes after writing. Do not close or mark FT-02 complete if publication or
either review remains incomplete.

FT-01 publishes the identical manifest members in its initial documentation root.
Review evidence and publication readback are separate artifacts, excluded from
their own content hash to avoid self-reference. Do not change normative packet
members after review; a change requires a new manifest and both final reviews.

## Remaining external evidence

IBKR eligibility, independent entitlements, retention/automated-use rights, actual
dated-contract/calendar mapping and supported gateway operation remain FT-03 work.
The user still needs to open or fund an account. A bounded depth recorder/pilot is
separate from candle-strategy readiness and needs its own packet and authority.
Actual Netcup capacity, encrypted off-server restore evidence, AG Charts licensing,
supported client round trips and measured release continuity remain their issue
gates. No synthetic test, documentation review or recommended budget satisfies
these external acceptance criteria.
