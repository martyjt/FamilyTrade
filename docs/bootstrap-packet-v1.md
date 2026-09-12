# FT-01 bounded commissioning packet v1

Planning only. FT-01 is the first implementation issue after the complete FT-02
packet receives both independent final-digest reviews. Nothing in this packet
starts a worker, commits files, or grants publication authority.

## Verified starting point

On 2026-09-12, `D:\Projects\FamilyTrade` has no HEAD commit; `git ls-remote origin`
returns no refs. Origin is `https://github.com/martyjt/FamilyTrade.git`, with `main`
configured as the default branch. All 19 GitHub issues are open and there are no
PRs. Existing files are untracked. Issue bodies match their local packets from
`## Problem` onward. Recheck these facts when commissioned.

## Exact scope and file audit

Publish the reviewed documentation bytes; establish installable backend/frontend
shells, locks, quality commands, CI, and development containers. Do not implement
domain schemas, strategies, fills, authentication, persistence or provider access.
The following audit is based on reading the files, without running the prototype.

| Existing file | Disposition for FT-01 | Reason |
| --- | --- | --- |
| `README.md` | Revise setup/status only | Planning entry point; currently has no verified setup commands |
| `docs/**` | Copy only the reviewed manifest and its review evidence | Contract baseline; do not recreate from chat summaries |
| `.gitignore` | Adapt deliberately | Preserve secret/data exclusions; add frontend/tool output exclusions |
| `.dockerignore` | Replace in isolated bootstrap checkout | Current allowlist copies only prototype source/examples |
| `pyproject.toml` | Replace in isolated checkout | Unlocked zero-dependency demo with CLI entry point; no accepted application tooling |
| `Dockerfile` | Replace in isolated checkout | Runs the unaccepted synthetic CLI |
| `compose.yaml` | Replace in isolated checkout | Offline demo only; no accepted development stack |
| `src/familytrade/__init__.py` | Recreate a minimal package marker | Existing description asserts a research core not delivered by FT-01 |
| `src/familytrade/domain.py` | Preserve locally, exclude from baseline | Template-only strategy and contiguous UTC-day assumptions conflict with contracts |
| `src/familytrade/strategies.py` | Preserve locally, exclude from baseline | Only channel breakout; strategy implementation belongs to FT-06/07 |
| `src/familytrade/simulation.py` | Preserve locally, exclude from baseline | Hardcoded immediate pending entry; fill implementation belongs to FT-07 |
| `src/familytrade/research.py` | Preserve locally, exclude from baseline | Caller-supplied owner and in-memory results are not FT-04/10 services |
| `src/familytrade/cli.py` | Preserve locally, exclude from baseline | Synthetic demo executable is outside bootstrap acceptance |
| `examples/lanes.json` | Preserve locally, exclude from baseline | Prototype configuration is not the frozen rule-builder vocabulary |

No deletion or overwrite of the original working directory is needed. A future
worker uses a separate checkout and individually stages allowed files. A later
issue may reuse an idea only after its own tests/review; this audit is not code
acceptance. Recheck for new/unrelated files before publication.

## Files to establish

Allowed new paths: `uv.lock`, `frontend/package.json`, `frontend/package-lock.json`,
`frontend/index.html`, `frontend/src/main.tsx`, `frontend/src/App.tsx`,
`frontend/tsconfig*.json`, `frontend/vite.config.ts`, `frontend/eslint.config.*`,
`frontend/src/App.test.tsx`, `.github/workflows/ci.yml`, `.gitattributes`, `.env.example`,
`src/familytrade/api.py`, `tests/bootstrap/test_shell.py`,
`tests/bootstrap/test_planning_packet.py`, `docs/development.md`.
Create additional package markers only where needed for these files. A minimal
health endpoint and a static "FamilyTrade — paper research" shell establish build
and request plumbing; no trading controls or application operations ship here.

Framework choices come from contracts-v1. FT-01 chooses mutually compatible pinned
package/runtime versions using primary documentation and records their versions in
`docs/development.md`. That bounded dependency resolution cannot change contracts.
Do not create empty speculative module hierarchies for every future issue.

## Empty-remote bootstrap and authority boundary

A PR cannot target an absent branch. Commissioning must explicitly authorise one
documentation-only root commit on `main`, containing the reviewed planning packet
and manifest/review evidence (plus `.gitattributes` to preserve their bytes), and
its push to the empty origin. Prepare that root
in a separate temporary repository/checkout, preserving the original directory.
Verify the remote is still empty immediately before the initial push; stop if it
is no longer empty and reconcile the baseline read-only. Do not force push.

After the root exists, create one `codex/ft-01-bootstrap` worktree/branch from its
recorded SHA. The worker adds only the bootstrap scope and opens one focused PR.
Review both the documentation root against the manifest and the implementation
candidate. Subsequent issues wait for explicit merge authority and verified
integration on `origin/main`. No second implementation issue starts in FT-01.
If root-publication authority is absent, deliver a local reviewed candidate and
report that exact boundary; do not invent an empty-base PR or silently push main.

## Acceptance commands and examples

FT-01 must establish these command interfaces and document prerequisites; they do
not exist yet and have not been run by planning:

```text
uv sync --frozen --all-groups
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/bootstrap
npm --prefix frontend ci
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend test -- --run
npm --prefix frontend run build
docker compose config --quiet
docker build -t familytrade-bootstrap-check .
```

Success: a clean Windows checkout and Linux CI install from locks, the health
endpoint returns the documented status, the static frontend renders, and every
reviewed planning file has the manifest SHA-256 after checkout. Preserve exact
UTF-8 bytes when copying; configure checkout line-ending handling for manifest
members and demonstrate it on both operating systems. Hashes must not depend on
platform newline rewriting.

Failure: mutate one copied fixture in a disposable test directory and show the
manifest check fails; restore it and show success. A missing lock dependency must
fail frozen install rather than rewrite locks. Demonstrate an intentional local
broken bootstrap assertion fails the same test command, then restore it. Never
push a deliberate failure or alter the original planning fixture to run this check.
Review image/staged-file contents for secrets, data and excluded prototype files.

No business-logic tests are required here. Checks must test shell/build/packet
integrity, not import unaccepted prototype modules or claim simulation correctness.

## Commissioning handoff

Queue: FT-01 / GitHub #2 only. Coordinator Sol/medium; worker Terra/medium;
Reviewer A Terra/medium and Reviewer B Sol/high, both fresh contexts. One mutating
worker. The worker returns validation logs, changed paths and frozen candidate SHA;
both reviewers inspect that SHA and the root packet digest. Changed candidates
need both reviews again. Stop at READY_FOR_MERGE; merge/deploy/provider work and
successor issues require separately supplied authority.

Paste this only when ready to commission implementation; it supplies authority
that the current planning request does not grant:

> Act as the thin FamilyTrade coordinator in D:\Projects\FamilyTrade for FT-01
> only: https://github.com/martyjt/FamilyTrade/issues/2. Read the final reviewed
> FT-02 manifest/reviews, docs/bootstrap-packet-v1.md and docs/session-workflow.md.
> Revalidate the empty remote and unchanged packet. Preserve the original untracked
> prototype. I authorise the documentation-only initial main commit and push
> described in the bootstrap packet, including byte-preservation attributes, then
> one isolated codex/ft-01-bootstrap worktree and worker branch. Delegate bootstrap
> implementation to Terra/medium. Use fresh independent Reviewer A Terra/medium
> and Reviewer B Sol/high on the identical final candidate and document-root digest.
> The worker may commit/push that branch and open one focused PR. Establish only
> the shell, pinned tooling, development containers and meaningful bootstrap checks.
> Do not implement domain services or start another issue. Stop at READY_FOR_MERGE;
> no merge, deployment, purchase, account operation, provider contact or live order
> is authorised. Return to planning on a material contract gap.
