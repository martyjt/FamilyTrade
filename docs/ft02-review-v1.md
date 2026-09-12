# FT-02 independent planning reviews

FINAL_VERDICT: PASS

No implementation is authorised by this review record. Reviewer evidence is
excluded from the content manifest to avoid self-reference; both reviewers must
inspect the same final manifest digest before freeze or publication.

## Candidate 1 — changes required

Packet digest: `3fb0627f511c4cad6ce5e3b12c9adb9b1d309e8e730f5492fe80cd101b3f7134`.
All 37 file hashes, byte lengths and the ordered aggregate were independently
recomputed successfully by both reviewers. Neither changed files or Git state.

Reviewer A: `/root/reviewer_a`, gpt-5.6-terra / medium, fresh context.
Verdict: CHANGES_REQUIRED. Scope: FT-02 acceptance, contracts/fixtures, arithmetic,
configurability, recipes, bootstrap, handoff and historical strategy provenance.
Reviewed arithmetic agreed for ATR/VWAP, long/short gaps, fees, P&L, drawdown,
gap-risk rejection, TTL and release timing.

Reviewer B: `/root/reviewer_b`, gpt-5.6-sol / high, fresh context.
Verdict: CHANGES_REQUIRED. Scope: clock causality/provenance, fills/risk, archive and
job fencing, lane controls/recovery, auth/shared operations, release rollback and
downstream readiness. B completed independently without receiving A's findings.

| Finding | Required resolution for candidate 2 |
| --- | --- |
| A P1: next_zone and r_multiple target modes incomplete | Freeze causal selection/formulas, no-target rejection, rounding and long/short fixtures |
| A P2: no passing non-null exit-root example | Add PASS-to-future-close and UNKNOWN-to-no-close examples |
| A P2: FT-01 documentation-in-first-PR wording | Specify documentation root publication before the bootstrap PR |
| B P1: forward availability conflicts with effective bar boundary | Separate execution_bar_end from causal forward effective/decision/commit times; delayed example |
| B P1: active forward bars lack published dataset revision | Freeze nullable published-base provenance plus exact immutable bar-record IDs |
| B P1: improved entry gap can invert bracket | Revalidate projected fill geometry before entry; cancel with no fees; symmetric fixtures |
| B P1: scheduler fire/next-fire not atomic | Strict schedule/job/attempt records, occurrence dedupe and one fire transaction; crash/DST/misfire fixtures |
| B P1: rollback cannot assume checkpoint readability | Freeze overlap read/write format policy and test rollback after a new-owner write |
| B P2: inconsistent correction event tags | One registered typed correction event for late observations |
| B P2: lane create/control results underspecified | Strict shared LaneCreate/LaneSnapshot/control projections |
| Coordinator: FT-07 shared-type dependency missing | Add FT-05 dependency in FT-07 recipe, issue and plan |

Changes are made by the planning author in a bounded batch, followed by a new
manifest and both independent final reviews. Previous-digest reviews do not approve
the changed packet. No external provider, account, VPS, client or trading evidence
is supplied by these planning reviews.

## Candidate 2 — earlier findings resolved; two further corrections

Packet digest: `15db5f79149071b462a48b0eab4731d3c037f2875ef85a73c3204caa6ce1271e`.
Both independent reviewers again verified all 37 file hashes/lengths and recomputed
the aggregate. The examples contained 34 unique cases; coverage and recipe links
resolved. They confirmed the candidate-1 findings and shared-type dependency were
resolved in the revised packet. No implementation or external evidence was inferred.

Reviewer A, same independent Terra/medium reviewer: CHANGES_REQUIRED for one new
P1. A generic market entry permits a risk-multiple target, but the executable entry
price is unknown at intent time. Freeze explicit reference/anchor semantics for
generic market stop/target specifications, preserving supported configuration, and
add a worked market-entry example.

Reviewer B, same independent Sol/high reviewer: CHANGES_REQUIRED for one new P1.
The broad existing-exits-before-close ordering allows a later intrabar bracket
touch to preempt a market close already eligible at the bar open. Resolve opening
bracket gaps first, then active market closes at open, then remaining intrabar
bracket tests. Add explicit stop/target levels to the exit-rule fixture and cases
for opening-gap precedence and a later touch losing to the opening market close.

The author makes these two corrections, then both reviewers inspect a newly
hashed candidate. A prior candidate's resolved findings do not constitute final
approval of changed bytes.

## Candidate 3 — final wording and fixture corrections

Packet digest: `d234ea0d2f160a67e13e2715df6e808d9cbe33464941d559735883608691189c`.
Both independent reviewers recomputed the aggregate and all 37 member hashes and
byte lengths. The examples parsed with 35 unique cases. Both confirmed the generic
market-entry template resolution and causal opening-close ordering fixes.

Reviewer A (Terra/medium): CHANGES_REQUIRED, P1. The delayed-close fixture had a
pre-existing target at 2005 and a 10:15 opening price of 2005, which must fill before
the close submitted after that open. Its zero-fill expectation was inconsistent.

Reviewer B (Sol/high): CHANGES_REQUIRED. B independently found the same P1 and a
P2 ambiguity: generic limit brackets were frozen at signal time in section 5.5,
while the prospective-fill template wording in section 13.2 lacked a market-only
restriction. Preserve the signal-time freeze for known limit entries and resolve
only market-entry bracket templates from the prospective actual fill.

The next candidate corrects the complete 10:15 OHLC to remain between its bracket
levels and aligns the market/limit wording. The normative opening-gap/market-close/
intrabar precedence remains unchanged. Both final reviews must cover the new bytes.
## Candidate 4 — final independent PASS

FINAL_PACKET_SHA256: 9bb0e22bb72ccc0dc1db56ddcb8ba9106b5260f92710b2597072247c3f5f723d

Date: 2026-09-12. Manifest file SHA-256:
`e776a31b3d9036e1e42180186d1f7367254e8fb4caade655052657b25cb423f5`.
Both reviewers independently recomputed the identical ordered packet digest and
verified all 37 member hashes and byte lengths. Neither edited files or Git state.

Reviewer A: `/root/reviewer_a`, gpt-5.6-terra / medium, independent read-only context.
Final verdict: PASS. All 35 cases parse. The complete 10:15 range stays inside its
bracket; the 10:16 close precedes the later intrabar target touch, and opening-gap
precedence remains correct. Generic limit brackets freeze from the signal-known
limit; market templates resolve from the prospective fill before atomic geometry/
risk checks. Companion arithmetic and FT-06/FT-07 bindings are consistent. No
material acceptance or cross-section inconsistency remains.

Reviewer B: `/root/reviewer_b`, gpt-5.6-sol / high, separate independent read-only
context. Final verdict: PASS. All 35 unique cases and coverage/recipe references
resolve; every case is used. The 10:16 close is 2004.70 and the opening stop-gap
subcase is 1993.90. Generic limit/market bracket semantics and numerical examples
are consistent. Earlier forward causality, corrections, schedules, archives, lane
recovery, authentication, release compatibility, configurability, dependency and
external/implementation gates remain intact. No remaining material finding.

All material findings from candidates 1–3 are resolved in this final reviewed
packet. Previous reviews did not approve changed bytes. Neither reviewer received
the other's findings during a review round; the author is a separate Sol/high
planning specialist. The coordinator incorporated review findings between rounds.

Coordinator validation: strict JSON duplicate-key check, 35 unique cases, all 81
recipe bindings (including ALL full-suite coverage), final fill/P&L/cash Decimal
arithmetic, and preservation checks passed. All 12 original prototype files and
five historical strategy files match their pre-task hashes. No prototype/application
test was run and no implementation was accepted. Publication readback is the last
freeze gate and is recorded separately in ft02-publication-v1.md.
