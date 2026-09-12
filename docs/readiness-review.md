# Backlog readiness review — 2026-09-12

This is the earlier gap review. The continuation packet in contracts-v1.md,
contracts-examples-v1.json and issue-recipes-v1.md addresses these named gaps.
See ft02-review-v1.md for independent inspection of the exact final manifest and
ft02-handoff-v1.md for current gated readiness. Do not infer a new review result
from this historical report.

A fresh gpt-5.6-sol/high subagent independently reviewed FT-02, FT-05, FT-07,
FT-08 and FT-11 for readiness for a gpt-5.6-terra/medium implementation worker.
This was read-only planning review; no implementation was performed.

## Findings

- FT-02 still assigned the consequential design choices to its worker. It needs
  concrete fields/invariants, event IDs, time/session rules, indicator/risk semantics,
  state machines, transaction boundaries and golden examples.
- FT-07 needs a fixed fill/accounting table for next-bar timing, long/short gaps,
  intrabar ambiguity, entry-bar exits, fees, pending expiry and open final positions.
- FT-05 needs a fixed manifest/revision/publication protocol and crash matrix;
  "atomic publication" across files and PostgreSQL is not a complete recipe.
- FT-08 needs job/claim/attempt states, lease/fencing/retry/cancel rules and exact
  overlap/misfire/DST/catch-up outcomes.
- FT-11 needs explicit start/pause/resume/stop/version-change transitions and a
  correction/staleness/replay policy, with an atomic checkpoint/crash matrix.

## Disposition

All five were not ready for lower-cost application implementation. FT-02 is now
a planning-only prerequisite without a dependency on the initial code bootstrap.
FT-01 must consume its reviewed packets; downstream coding issues remain
WAIT_CONTRACTS_AND_DEPENDENCIES until their concrete inputs are available.

The orchestration workflow and dispatch profiles are now explicit. This change
does not claim the missing contracts have been written or validated. The next
commissioned specialist task is FT-02 planning, followed by independent reviews.
Do not dispatch the application backlog merely because issue titles and broad
acceptance criteria exist.

The reviewed scope was the five named issues, not an independent certification
of all seventeen. Every coding issue still passes the readiness checklist in
[the session workflow](session-workflow.md) before dispatch.
