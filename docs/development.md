# FamilyTrade development bootstrap

FT-01 starts from documentation root `5aa4e7766846e889d45ac87c5da80db796869017`.
The reviewed README and all 37 files named by `docs/planning-manifest-v1.json` are
immutable in this bootstrap branch. `.gitattributes` keeps those files as exact
bytes across Windows and Linux checkouts; `tests/bootstrap/test_planning_packet.py`
recomputes every member hash, byte length and ordered packet digest.

## Prerequisites

Use Python 3.14.3, Node.js 24.14.0, npm 11.9.0, Docker with Compose, and uv 0.12.13. On
Windows, Docker Desktop must be switched to Linux containers for the supplied
Linux image build and Compose runtime smoke. The Windows CI job retains the locked
install, code, packet, frontend, and Compose-configuration checks; Linux CI builds
and runs the containers. The
Python dependencies are exact pins, including FastAPI 0.141.1, Pydantic 2.13.5,
SQLAlchemy 2.0.52, Alembic 1.20.0, psycopg 3.3.5, Authlib 1.8.0, and MCP 2.2.0.
MCP 2.2.0 is the stable v2 SDK line for the 2026-07-28 protocol. The bootstrap test
imports its v2 client/server/schema surface; FT-15 owns authenticated interoperability.
The frontend pins TypeScript 5.9.3 because the maintained OpenAPI generator requires
TypeScript 5.x while TypeScript ESLint 8.70.0 also accepts that range.
`pwdlib[argon2]` 0.3.0 and the `openapi-typescript` 7.13.0 generator are pinned for the later
local-password and generated-client seams; FT-01 does not implement either flow.

## Commands

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

Copy `.env.example` to `.env` only for local configuration; it contains no
credentials. Run `docker compose up --build` to start the API at
`http://localhost:8000/health` and the Vite development shell at
`http://localhost:5173`.

`uv sync --frozen` treats the committed lock as immutable. The FT-01 disposable
probe removes the referenced FastAPI entry from a copy of `uv.lock`; the command
must exit nonzero and leave that copied lock byte-for-byte unchanged. Update locks
deliberately with `uv lock`, then commit the resulting `uv.lock`; never rely on a
frozen install to repair it.

After `docker compose up --build` has made the API available, run
`npm --prefix frontend run generate:openapi` to generate its OpenAPI client.
