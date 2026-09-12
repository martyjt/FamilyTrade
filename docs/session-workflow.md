# Thin orchestration with issue workers and independent reviewers

This is the implementation workflow requested for FamilyTrade. It does not start
implementation by itself. The planning session prepares issues and resolves their
contracts; a separately commissioned orchestration session dispatches ready work.

Current FT-02 readiness is governed by `ft02-handoff-v1.md` and the exact-digest
independent reviews in `ft02-review-v1.md`. Once that gate passes, use the concrete
FT-01 bootstrap packet as the first commissioning candidate. Historical
READY_FOR_PLANNING wording below is not its current completion evidence. No
implementation is commissioned merely by completing planning.

## Roles and model defaults

These are project recommendations using models exposed by the current runtime,
not claims about universal pricing or benchmark superiority. Validate availability
when starting a session; never silently inherit the coordinator's expensive model.

| Role | Default | Responsibility |
| --- | --- | --- |
| Thin coordinator | gpt-5.6-sol / medium | Check readiness/dependencies, dispatch, consolidate findings and report state |
| Bounded implementation worker | gpt-5.6-terra / medium | Implement the issue's frozen specification and relevant tests |
| Sensitive implementation worker | gpt-5.6-sol / medium | Security, persistence, concurrency, provider recovery or fill accounting |
| Contract/planning specialist | gpt-5.6-sol / high | Resolve explicitly identified design questions before coding |
| Reviewer A | gpt-5.6-terra / medium | Independently verify acceptance criteria, tests and concrete examples |
| Reviewer B, routine issue | gpt-5.6-sol / high | Review correctness, simplicity, regressions and module boundaries |
| Reviewer B, sensitive issue | gpt-5.6-sol / high | Review money, isolation, recovery, concurrency and adversarial failure cases |

Luna/low is optional for a genuinely mechanical, separately bounded task such as
extracting a status summary. It is not the default financial/security reviewer.
Escalate reasoning for a named unresolved problem, not because a task is long.
Use Sol/high or xhigh for a difficult review only when the concrete risk warrants
it. Astra is an exception for an unresolved consequential problem, not a routine
worker or reviewer. Record escalation reason and result in the issue handoff.

Model/effort pairs are explicit spawn settings. Use fresh brief-only contexts and
exact issue/code pointers instead of inheriting the planning chat. This host
currently allows four concurrent agents including the coordinator: one writer and
two independent reviewers fit, although reviewers inspect only a frozen revision.
Do not change global Codex settings or create a scheduler for this workflow.

## An issue being written does not mean it is ready

Work through product/trading choices with the user during planning; do not delegate unresolved business decisions to a coding worker. Routine engineering details may be resolved within the reviewed architecture.

Before dispatching a coding worker, the issue must contain or link to:

1. A frozen behaviour contract, required inputs/outputs, types and error semantics.
2. Exact ownership/seams in the established repository; allowed and excluded changes.
3. At least one concrete success example and relevant failure/boundary examples.
4. Testable acceptance criteria, test fixtures, required validation commands and
   explicit external evidence that local tests cannot replace.
5. Available dependencies at recorded integrated revisions, plus the applicable
   reviewed contract version. Planning-only dependencies may instead be satisfied
   by their reviewed, recorded specification before the initial Git bootstrap.
6. Assigned worker/reviewer model and effort, risk focus and escalation condition.
7. No unresolved decision that materially changes money, access, data or execution.

If any of these are missing, mark WAIT_CONTRACTS or WAIT_DEPENDENCIES and return
the precise gap to planning. Do not ask a cheaper coding agent to design its way
around it. FT-02 is READY_FOR_PLANNING, not an application implementation issue.
Its contract packets precede FT-01's repository bootstrap.

## Coordinator responsibilities

- Read the user-authorised queue and delivery authority, then inspect only the
  needed issue/dependency/base state.
- Keep one mutating issue active by default. Parallel read-only reviews are allowed;
  parallel issue implementation requires explicit authorisation and disjoint seams.
- Select the issue's explicit worker profile and give it a compact task brief,
  issue URL, reviewed contract/fixtures, worktree path and acceptance commands.
- Keep coding, broad repository discovery and verbose test logs with workers.
  Return to planning if the worker discovers material ambiguity.
- Do not launch workers merely to wait or relay messages. Use completion waits and
  concise summaries; no autonomous watcher, claim service or successor chain.
- Preserve the exact authorised queue. Do not silently skip a blocked issue or
  extend the queue; only continue independent work if that was authorised.

The user does not need to open a separate visible task for every issue. One thin
orchestration session can coordinate the approved queue, with fresh worker and
reviewer subagents per issue. Use a new orchestrator session when useful for context,
with a compact handoff in the existing parent issue; do not add a tracking platform.

## Worker and review cycle

1. Verify readiness and establish a codex/ branch/worktree from the recorded shared
   base. For the initial empty repository, perform the documented FT-01 bootstrap
   only after the FT-02 planning packets are reviewed. Keep local experiments out
   unless individually justified by the issue.
2. Worker implements and tests only the issue, then returns changed paths, validation,
   assumptions and a frozen candidate commit SHA. If commit authority is absent,
   return a local diff for review and do not claim PR delivery.
3. Stop mutations while Reviewer A and Reviewer B independently inspect the same
   candidate SHA/base. Give each the issue, contracts, fixtures and diff pointers;
   the worker's summary is supporting context, never sole evidence. Reviewers do
   not fix code or reuse the implementation agent as the independent reviewer.
4. Each reviewer returns SHA, scope checked, findings with file/line and severity,
   evidence, and PASS / CHANGES_REQUIRED / BLOCKED. Each has a distinct focus.
5. Route findings to the original worker for a bounded fix batch. Resolve material
   design questions in the issue, not ad hoc in coordinator code. Any changed
   candidate requires both reviewers to inspect the final revision; prior-head
   approvals do not cover it.
6. Check applicable CI and review evidence against the exact final PR head. Open or
   update the focused PR only within the commissioned commit/push/PR authority.
   No external review service is required by this workflow.
7. Report READY_FOR_MERGE when review/CI pass. Merge only when explicitly authorised;
   otherwise stop with the PR and unmet authority/dependencies. Verify integration
   before closing the issue or treating it as a code dependency.

Subagents share the filesystem. Use explicit worktree paths, one writer, and a frozen
candidate during reviews; a requested read-only role is not filesystem isolation.
Do not discard unrelated changes or weaken tests for completion.

## Compact handoff

Record only: issue and state; base/candidate SHA; branch/worktree/PR; worker and
reviewer model/effort; acceptance/test outcomes; open findings or external evidence;
next authorised action. Preserve supporting logs as artifacts, not coordinator chat.
This is ordinary issue/PR documentation, not a custom checkpoint system.

## Commissioning prompt

Use this only when the user is ready to start implementation orchestration:

> Act as the thin FamilyTrade coordinator for this explicit queue: <issue URLs>.
> Use docs/session-workflow.md and the matching dispatch profiles in each issue.
> Verify readiness and the reviewed contracts before spawning workers. Delegate
> implementation to the assigned worker model/effort and use fresh independent
> Reviewer A and Reviewer B agents on the exact final revision. Keep one mutating
> issue active; do not implement application code in the coordinator.
> You may create isolated branches/worktrees, and have the worker commit/push the
> issue branch and open a focused PR. Do not merge, deploy, purchase services,
> contact providers, place orders or extend the queue. Stop at missing contracts,
> dependencies, external evidence or merge authority and report the exact next step.

For the next planning step, use a planning-only brief for FT-02 instead. No code,
commit/push/PR or deployment authority is implied by an issue's dispatch profile.

## Reference

Official OpenAI documentation supports explicit model/effort selection and separate
worker/reviewer roles; the particular project assignments above are our choices:
https://learn.chatgpt.com/docs/agent-configuration/subagents
