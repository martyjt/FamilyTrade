# FT-06 implementation contract v1 amendment 1

Status: CONTRACT_AMENDMENT_CANDIDATE_FOR_IDENTICAL_BYTE_REVIEW.

This additive amendment closes one CI-integration gap only. It neither changes the
approved FT-06 contract nor alters any frozen FT-02 member, strategy behavior,
application code, schema, test meaning, or FT-07 boundary.

## 1. Evidence and immutable input

The approved implementation head is
edf6bb8403cbebde01563cf4295a06d32ab74bf0. Pull request 23 CI run 34879323628
failed bootstrap on both ubuntu-latest (job 104094362486) and windows-latest (job
104094362410) at the repository-wide formatting check.

At that head, Ruff 0.16.7 reports only
docs/ft06-implementation-contract-v1.md as unformatted. It would insert blank lines
inside the Python example fence at source lines 133, 143, 146, and 149. The file is
an identical-byte-approved contract and must not be reformatted.

Its immutable committed fingerprint is exactly:

| property | value |
| --- | --- |
| Git blob | 6a7f6dac78c13d7cd140a2bab6fc48716009339c |
| SHA-256 | d27368178c51aed3588d727f6a1f2776008f97ce2c9beaa3a1d9b4d46374ab09 |
| bytes | 55,404 |
| LF | 1,030 |
| CR | 0 |
| trailing LF | yes |

The current command remains successful when that one immutable path is excluded:
72 other files are already formatted, and `ruff check .` passes.

## 2. Narrow precedence and allowlist amendment

This document supplements only section 11 of
docs/ft06-implementation-contract-v1.md. Every other requirement, precedence rule,
allowed-path restriction, test, and non-goal in that approved contract remains
unchanged and higher-level frozen sources remain authoritative.

The implementation allowlist gains exactly:

- docs/ft06-implementation-contract-v1-amendment-1.md, immutable after
  identical-byte approval.
- .github/workflows/ci.yml, only the one bootstrap-job command replacement below.

In the existing bootstrap matrix job, replace exactly:

~~~text
- run: uv run ruff format --check .
~~~

with exactly:

~~~text
- run: uv run ruff format --check --exclude docs/ft06-implementation-contract-v1.md .
~~~

The adjacent `uv run ruff check .` command remains byte-for-byte unchanged. No
other workflow command, job, trigger, matrix, permission, dependency, or path may
change under this amendment. pyproject.toml is not added to the allowlist and no
repository-wide Ruff configuration exclusion is authorized.

The exclusion names one repository-relative file, so every other Python file and
every other supported code block remains subject to the global formatting check.
It is not a pattern for docs, Markdown, FT-06 files, or future amendments.

## 3. Required implementation validation

After the authorized workflow edit, all of these commands must succeed from the
repository root:

~~~text
uv run ruff check .
uv run ruff format --check --exclude docs/ft06-implementation-contract-v1.md .
uv run mypy src
uv run pytest tests/bootstrap
uv run pytest tests/access
uv run pytest tests/market_data
uv run pytest tests/strategies/test_definitions.py
~~~

The immutable contract must also pass this exact byte gate:

~~~text
uv run python -c "from hashlib import sha256; from pathlib import Path; b=Path('docs/ft06-implementation-contract-v1.md').read_bytes(); assert sha256(b).hexdigest()=='d27368178c51aed3588d727f6a1f2776008f97ce2c9beaa3a1d9b4d46374ab09' and len(b)==55404 and b.count(b'\n')==1030 and b.count(b'\r')==0 and b.endswith(b'\n')"
~~~

The implementation diff from
edf6bb8403cbebde01563cf4295a06d32ab74bf0 must contain only this amendment and the
single authorized ci.yml line replacement. The approved contract and all 37
frozen manifest members must have zero byte changes; the frozen aggregate remains
9bb0e22bb72ccc0dc1db56ddcb8ba9106b5260f92710b2597072247c3f5f723d.

No application implementation, formatter weakening, test removal, FT-07 behavior,
deployment, provider, purchase, or live-order authority is granted.
