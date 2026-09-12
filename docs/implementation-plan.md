# FamilyTrade implementation plan

Status: planning backlog published on 2026-09-12. No implementation session is authorised
by this document alone.

Current continuation: [FT-02 handoff](ft02-handoff-v1.md),
[contracts](contracts-v1.md), [worked examples](contracts-examples-v1.json) and
[issue recipes](issue-recipes-v1.md) supersede historical pending-default statements
below after both final-digest reviews in [the review record](ft02-review-v1.md) pass.
Then FT-01 is READY_FOR_COMMISSIONING, with its [bounded bootstrap packet](bootstrap-packet-v1.md).
Other issues wait for integrated dependencies and named external evidence. This
planning continuation does not start implementation or commission a successor queue.

- [Planning parent](https://github.com/martyjt/FamilyTrade/issues/1)
- [First-slice milestone](https://github.com/martyjt/FamilyTrade/milestone/1)
- [First implementation issue: repository foundation](https://github.com/martyjt/FamilyTrade/issues/2)

The backlog now contains 19 issues: one parent, one planning child and seventeen
implementation/evidence issues, including release continuity FT-18. Dependencies are explicit links in each
issue body; the parent contains the complete task list. No implementation sessions have been started.

## Outcome

A private application for fewer than five users: independently licensed and stored
futures data, configurable strategies, reproducible backtests, persistent forward
paper lanes, AG Charts analysis and authenticated MCP access. Start with Micro Gold
and one-minute bars. Live orders remain outside this milestone.

The architecture is a modular Python application with a React/TypeScript interface,
PostgreSQL operational state, per-user Parquet archives and Docker Compose on one
Netcup server. Pure strategy/simulation functions are independent of I/O. Use
maintained native libraries before considering custom Rust.

## First-slice boundary

Accepted on 2026-09-12: a bounded rule builder from the start, and overnight positions.
Users combine supported indicators/conditions for entry and exit; breakout/crossover
are optional presets rather than a template-only restriction. Use typed, validated,
bounded rule structures with immutable saved versions, shared by UI and MCP.

FT-02 freezes the indicator/operator catalogue, numerical/fill/risk semantics and
overnight lifecycle after the remaining user decisions. Entry session filters,
daily-risk resets and holding positions overnight must be separate controls. No
arbitrary user code, silent expiry rolls or profit-based live activation is added.

The user still needs to open or fund an IBKR account; provider proof remains an
external dependency. See [the decision record](planning-decisions.md).

## Delivery backlog

| Key | Deliverable | Depends on |
| --- | --- | --- |
| [FT-01](https://github.com/martyjt/FamilyTrade/issues/2) | Bootstrap the repository and reproducible development tooling | [FT-02](https://github.com/martyjt/FamilyTrade/issues/3), reviewed planning output |
| [FT-02](https://github.com/martyjt/FamilyTrade/issues/3) | Freeze contracts and golden examples (planning) | None for planning |
| [FT-03](https://github.com/martyjt/FamilyTrade/issues/4) | Prove broker and data access locally and on the Netcup server | [FT-02](https://github.com/martyjt/FamilyTrade/issues/3), [FT-01](https://github.com/martyjt/FamilyTrade/issues/2) |
| [FT-04](https://github.com/martyjt/FamilyTrade/issues/5) | Implement user identity, broker-account ownership and credential storage | [FT-02](https://github.com/martyjt/FamilyTrade/issues/3), [FT-01](https://github.com/martyjt/FamilyTrade/issues/2) |
| [FT-05](https://github.com/martyjt/FamilyTrade/issues/6) | Implement independent candle archives and reproducible dataset revisions | [FT-02](https://github.com/martyjt/FamilyTrade/issues/3), [FT-04](https://github.com/martyjt/FamilyTrade/issues/5) |
| [FT-06](https://github.com/martyjt/FamilyTrade/issues/7) | Implement validated and versioned strategy definitions | [FT-02](https://github.com/martyjt/FamilyTrade/issues/3), [FT-04](https://github.com/martyjt/FamilyTrade/issues/5) |
| [FT-07](https://github.com/martyjt/FamilyTrade/issues/8) | Build the deterministic decision and paper-fill engine | [FT-02](https://github.com/martyjt/FamilyTrade/issues/3), [FT-05](https://github.com/martyjt/FamilyTrade/issues/6), [FT-06](https://github.com/martyjt/FamilyTrade/issues/7) |
| [FT-08](https://github.com/martyjt/FamilyTrade/issues/9) | Implement durable scheduled jobs and bounded worker execution | [FT-02](https://github.com/martyjt/FamilyTrade/issues/3), [FT-04](https://github.com/martyjt/FamilyTrade/issues/5) |
| [FT-09](https://github.com/martyjt/FamilyTrade/issues/10) | Integrate ongoing futures ingestion and scheduled gap repair | [FT-03](https://github.com/martyjt/FamilyTrade/issues/4), [FT-05](https://github.com/martyjt/FamilyTrade/issues/6), [FT-08](https://github.com/martyjt/FamilyTrade/issues/9) |
| [FT-10](https://github.com/martyjt/FamilyTrade/issues/11) | Persist backtest runs and execute them as owned background jobs | [FT-05](https://github.com/martyjt/FamilyTrade/issues/6), [FT-06](https://github.com/martyjt/FamilyTrade/issues/7), [FT-07](https://github.com/martyjt/FamilyTrade/issues/8), [FT-08](https://github.com/martyjt/FamilyTrade/issues/9) |
| [FT-11](https://github.com/martyjt/FamilyTrade/issues/12) | Run persistent forward-paper lanes with restart recovery | [FT-07](https://github.com/martyjt/FamilyTrade/issues/8), [FT-09](https://github.com/martyjt/FamilyTrade/issues/10), [FT-10](https://github.com/martyjt/FamilyTrade/issues/11) |
| [FT-12](https://github.com/martyjt/FamilyTrade/issues/13) | Provide strategy metrics, comparisons and decision evidence | [FT-10](https://github.com/martyjt/FamilyTrade/issues/11) |
| [FT-13](https://github.com/martyjt/FamilyTrade/issues/14) | Build the private app shell and strategy/run controls | [FT-04](https://github.com/martyjt/FamilyTrade/issues/5), [FT-06](https://github.com/martyjt/FamilyTrade/issues/7), [FT-10](https://github.com/martyjt/FamilyTrade/issues/11) |
| [FT-14](https://github.com/martyjt/FamilyTrade/issues/15) | Add AG Charts and paper-strategy comparison screens | [FT-05](https://github.com/martyjt/FamilyTrade/issues/6), [FT-11](https://github.com/martyjt/FamilyTrade/issues/12), [FT-12](https://github.com/martyjt/FamilyTrade/issues/13), [FT-13](https://github.com/martyjt/FamilyTrade/issues/14) |
| [FT-15](https://github.com/martyjt/FamilyTrade/issues/16) | Expose authenticated research and paper-control MCP tools | [FT-04](https://github.com/martyjt/FamilyTrade/issues/5), [FT-06](https://github.com/martyjt/FamilyTrade/issues/7), [FT-10](https://github.com/martyjt/FamilyTrade/issues/11), [FT-11](https://github.com/martyjt/FamilyTrade/issues/12), [FT-12](https://github.com/martyjt/FamilyTrade/issues/13) |
| [FT-16](https://github.com/martyjt/FamilyTrade/issues/17) | Package and validate the single-VPS deployment and backups | [FT-09](https://github.com/martyjt/FamilyTrade/issues/10), [FT-10](https://github.com/martyjt/FamilyTrade/issues/11), [FT-11](https://github.com/martyjt/FamilyTrade/issues/12), [FT-14](https://github.com/martyjt/FamilyTrade/issues/15), [FT-15](https://github.com/martyjt/FamilyTrade/issues/16) |
| [FT-18](https://github.com/martyjt/FamilyTrade/issues/19) | Prove seconds-level application releases and safe rollback | [FT-16](https://github.com/martyjt/FamilyTrade/issues/17) |
| [FT-17](https://github.com/martyjt/FamilyTrade/issues/18) | Accept the complete two-user paper slice and record remaining gaps | [FT-03](https://github.com/martyjt/FamilyTrade/issues/4), [FT-09](https://github.com/martyjt/FamilyTrade/issues/10), [FT-10](https://github.com/martyjt/FamilyTrade/issues/11), [FT-11](https://github.com/martyjt/FamilyTrade/issues/12), [FT-12](https://github.com/martyjt/FamilyTrade/issues/13), [FT-14](https://github.com/martyjt/FamilyTrade/issues/15), [FT-15](https://github.com/martyjt/FamilyTrade/issues/16), [FT-16](https://github.com/martyjt/FamilyTrade/issues/17), [FT-18](https://github.com/martyjt/FamilyTrade/issues/19) |

Each published issue contains the problem, scope, exclusions, acceptance criteria
and validation. Dependencies mean the required contracts/behaviour must be available
on the shared baseline before that coding worker begins (FT-02's reviewed planning output is the pre-bootstrap exception); an issue being
open or a PR merely existing does not satisfy a dependency.

## Work available while Netcup is pending

After FT-02 planning and the repository baseline, access, strategy definitions,
storage, durable jobs and the pure paper engine can progress with synthetic/test
inputs. The broker feasibility issue can collect account/terms evidence before VPS
provisioning but cannot close until its server/session criteria are demonstrated.

Real provider ingestion waits for FT-03. The end-to-end acceptance issue waits for
actual provider, user and VPS evidence. Synthetic results must always retain their
label, and cannot be used to close those external requirements.

## Sessions and integration

A thin orchestration session dispatches issue workers and two independent reviewers.
It does not implement application code. The user need not open one visible task per
issue. Use fresh subagent contexts, explicit model/effort profiles and one mutating
issue/worktree at a time, following the [session workflow](session-workflow.md).

Routine bounded workers use Terra/medium; sensitive persistence, security, recovery
and fill work uses Sol/medium. Reviewer A uses Terra/medium; Reviewer B uses Sol/high on an independent review. FT-02 is planning work for
Sol/high with independent reviews. Escalate only for a concrete unresolved problem.

The [independent readiness review](readiness-review.md) found that core issues still
leave consequential policy choices open. FT-02 is READY_FOR_PLANNING; application
issues are WAIT_CONTRACTS or WAIT_CONTRACTS_AND_DEPENDENCIES. This is not a claim that
all coding issues are already executable by a lower-cost worker.

FT-02 has no code dependency: freeze reviewed planning packets first. FT-01 then
bootstraps the Git baseline and publishes those exact packets. Before each coding
dispatch, fill exact paths/symbols, examples and validation commands from that base.
Reviewers inspect the same frozen candidate; changed code requires final-head review
and applicable CI. Merge/deploy authority remains separate from planning.

## Current prototype

The local src/, examples/, pyproject.toml, Dockerfile, compose.yaml, .dockerignore and
.gitignore were created during an interrupted exploratory turn. They are untracked,
untested and not an accepted implementation. This planning pass preserves them.
FT-01 must inspect each candidate file; there is no authority to bulk-adopt or delete
them. The existence of a Dockerfile does not prove build/deployment readiness.

The planning documents are also local until published in a future baseline PR.
The GitHub parent issue and child issue bodies carry the self-contained planning
contract so new sessions do not depend on this chat.

## Decisions and evidence still required

| Question | Resolution owner |
| --- | --- |
| Exact framework/package versions and shared Git baseline | FT-01 |
| Strategy/session/risk rules and inter-module request/event contracts | FT-02 |
| Provider/API eligibility, retention and automated-use permissions | FT-03 |
| Actual Netcup plan, supported gateway login and session resource use | FT-03, then FT-16 |
| Supported authenticated MCP transport/client setup | FT-15 |
| Observed capacity, recovery point/time and restore evidence | FT-16 and FT-17 |

Do not infer provider permission from two subscriptions or infer unattended operation
from a restart setting. Keep missing account/server evidence explicit. Never include
credentials, private licence material or raw account identifiers in GitHub issues.

## Subsequent work

After accepting the paper slice, plan live futures execution separately: order intent
persistence, uncertain-submit reconciliation, broker/account risk limits, emergency
stop and account/contract lane ownership. Initial market-structure, level and volume primitives are settled in FT-02;
further primitives and deeper data are additions justified by measured needs.
Zero planned routine-release downtime follows the initial seconds-level target;
see [the release plan](release-continuity.md). No automatic transition
from a profitable paper run to live orders is planned.

## User decisions

Work through material choices with the user before the contract freeze. See [the decision record](planning-decisions.md). Rule-builder, overnight scope, both paper controls, verified automatic recovery
and seconds-level releases are accepted. Exact feature definitions, fill/risk
defaults and numerical continuity budgets remain pending.

## First strategy packet

The user has authorised planning the bentradingbot Reversal–Breakout family as a
configurable FamilyTrade strategy. The [strategy draft](strategies/reversal-breakout-v1.md)
and [synthetic worked examples](strategies/reversal-breakout-examples-v1.json) are
one FT-02 packet, not the entire frozen contract. Source-default presets and causal
FamilyTrade execution differences must be distinguished; no live chart settings or
historical profit parity are inferred. The GitHub FT-02 body carries a complete
copy while the local repo has no published baseline.

The builder needs bounded zone/setup/retest/expiry operations in addition to typed
indicator conditions. This is a concrete requirement of the requested strategy,
not permission to build a general visual workflow engine. FT-06, FT-07, FT-11,
FT-12, FT-13 and FT-15 consume the final accepted packet through their existing gates.
