# Reversal–Breakout draft review record

Date: 2026-09-12. Result: **two independent PASS results for a reviewable draft**.
This is not user acceptance of the proposed trading behaviour, a complete FT-02
contract freeze, implementation readiness, or a performance validation.

## Final reviewed packet

| File | SHA-256 |
| --- | --- |
| reversal-breakout-v1.md | d4eae1e10fefdb3287c7518f301632dcf9c60c3b11d62f3324aadd851e47d3f5 |
| reversal-breakout-examples-v1.json | fd62857956ffae7eb9b5cd7b07be10054ad4b16b8654e629c8efdaba3aa7ac56 |

The source repository was read only and remains clean at
`8cd1cbc6d8c2306e7c3c731d159c98b255efda31`. Its two source hashes are recorded in
the specification and were independently verified against the files.

## Review evidence

- Writer: `breakout_spec_writer`, gpt-5.6-sol / high; owned only the specification
  and worked-example JSON. No application code, imports, broker operations, Git
  mutations or deployment were performed.
- Reviewer A: `breakout_review_a`, gpt-5.6-terra / medium, independent context.
  PASS after checking executable source defaults, provenance hashes, 12 unique
  parseable fixtures and hand calculations, bounded configuration and proposal
  labels. Verified both final packet hashes after the last disclosure edit.
- Reviewer B: `breakout_review_b`, gpt-5.6-sol / high, independent context.
  Initial CHANGES_REQUIRED identified filter/rejection states, simultaneous opposing
  arms, ATR reference/fallback, incomplete disclosure of eligibility changes and
  recent-extreme window rules. The original writer fixed these and added examples.
  A second review requested the acceptance paragraph include all material deltas.
  Final PASS verified both final hashes and resolution of the last finding.

The final fixtures cover long/short geometry and future fill eligibility, optional
directional/strict-cross conditions, setup expiry, pivot confirmation, same-bar
exclusion, opposing arms, filter rejection, ATR modes/reference, cooldown/daily cap
and the recent-extreme stop window. They deliberately do not invent unapproved
fill accounting or claim provider/exchange evidence.

## Remaining decisions

The proposed reference is the original-default MGC preset, with both reversal and
breakout plus a separately runnable breakout-only variant. Funded-v2 source defaults
are retained as a distinct reference. Neither represents verified live chart settings.
New structure/volume/ADX filters default off until their own definitions are frozen.

The strategy draft names the proposed execution differences from Pine, including
completed lower-timeframe processing, later retests/future fills, frozen setup
prices, conflicts, TTL, warm-up rejection and durable entry limits. User acceptance
of the baseline/execution direction is still pending. Routine deterministic details
have recommendations; the user is not expected to invent indicator equations.

Wider FT-02 fill/cost, calendar, expiry, event/checkpoint and recovery contracts remain
open, as do FT-03 account/provider/retention evidence. No GitHub issue is closed or
made ready for coding by these PASS results.
