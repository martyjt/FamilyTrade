# Architecture and decisions

The precise v1 behaviour and engineering choices are in [contracts-v1](contracts-v1.md).
Read [the current handoff](ft02-handoff-v1.md) for its final review gate and
precedence over earlier provisional language here. The architecture and accepted
scope below remain in force; no prototype code is adopted by the contract freeze.

## Scope

FamilyTrade supports a private group of fewer than five users, initially trading
exchange-traded futures. Micro Gold is the first instrument. Multi-user support
provides separation, not billing or public distribution.

One-minute candles are the initial canonical research interval. Larger intervals
are derived from them using exchange sessions. A provider may deliver finer live
events without FamilyTrade retaining a permanent fine-grained archive. Strategies
requiring order-book data or precise intrabar sequencing are outside the first slice.
The accepted data direction now includes a bounded per-user Level 2 recording pilot
after provider/retention proof. This builds future research data; order-book signals
and a permanent full-depth archive are not prerequisites for the candle-based slice.
Freeze pilot byte/time limits and reset/gap semantics before implementing collection.

## Application structure

Use one repository and a modular application, with separate processes for workloads
that need isolation. Start with:

- Python application logic and workers, using native data libraries such as Polars.
- React/TypeScript and the existing AG Charts licence for the web interface.
- PostgreSQL for users, strategy versions, runs, durable jobs and operational state.
- Per-user Parquet files for completed historical candle partitions, with a catalog
  in PostgreSQL. Keep the current ingestion window in PostgreSQL and publish archive
  partitions atomically; readers must not see duplicate active/archive records.
- Docker Compose on a single Linux VPS, subject to broker gateway compatibility.

Do not start with custom Rust, a distributed service system, a separate message
broker, or an AI orchestration framework. Profile representative workloads before
adding infrastructure or another implementation language.

| Module | Owns |
| --- | --- |
| Access | User identity, credentials, authorisation and isolation |
| Market data | Provider adapters, contract metadata, ingestion, archive and quality |
| Strategies | Versioned definitions, validation, indicators and decision functions |
| Simulation | Historical replay, forward-paper fills and lane state |
| Execution | Broker adapters, order lifecycle, risk checks and reconciliation |
| Analysis | Run metrics, comparisons and decision evidence |

Web and MCP entry points call the same application operations. Start with an API
process, a continuously running ingestion/paper worker, and a resource-limited
backtest worker. As live trading is introduced, give execution its own process.
Durable job records prevent work from disappearing when a process restarts.

## Functional programming style

Keep indicators, signal evaluation, sizing and metric calculations as pure functions
where practical. A decision step takes explicit configuration, prior state and a
market event, and returns new state and decision records. It performs no network or
database calls. Use typed immutable values where this keeps the code clear.

Keep clocks, random seeds, persistence and broker calls at the boundaries. Small
adapter objects are acceptable; avoid inheritance hierarchies and global mutable
state. Historical and forward runs use the same decision functions.

## User isolation and data ownership

Each user has independent subscriptions, feed sessions, credentials and physical
market-data storage. Do not share or deduplicate market data between users.
Within a user, all strategy lanes reference the same eligible datasets.

Derive the acting user from authenticated identity, not a user ID supplied by a
browser or MCP argument. Scope database records, queued jobs, file paths, exports
and credentials to that identity. Use server-generated storage paths and verify
ownership on every resource access. Private data and credentials must not enter logs.

Provider terms must permit the intended retention and automated use even with
separate subscriptions. Do not treat ordinary display subscriptions as proof of
those rights. Confirm permissions before collecting a permanent archive.

## Ingestion and futures data

Maintain a persistent connection for forward data where the provider supports it.
Use scheduled durable jobs for historical backfills, gap repair, quality checks
and archive maintenance. Schedules are application functionality, not Codex tasks.

Track progress per user, source, actual contract and interval. Apply rate limits,
bounded retries and duplicate-safe writes. Recover from the last durable progress
marker. Separate developing bars from finalised bars; strategies act on completed
bars by default. Define provider-specific completion and correction handling.

Store source timestamps in UTC alongside exchange session/calendar metadata.
Preserve contract identity, expiry, tick size, multiplier and currency. Represent
tradable prices using exact tick/decimal rules. A root symbol such as MGC does not
uniquely identify a tradable contract.

Flag missing, stale, duplicate and invalid bars without inventing market activity
for gaps. Data corrections create traceable revisions. Freeze or retain the data
revision needed to reproduce completed runs. Continuous futures are a derived
research view with an explicit roll/adjustment policy; orders use actual contracts.

## Configurable strategies and lanes

The user selected a bounded rule builder and overnight positions for the first
release on 2026-09-12. Users combine supported indicators and conditions into entry
and exit rules without code changes. Breakout and moving-average crossover are
examples/presets, not the only supported strategy families. Typed rule nodes,
condition groups, validation and complexity limits are defined in the planning-only
FT-02 contract; no arbitrary code or general visual workflow engine is introduced.

Overnight positions persist across session boundaries. Entry windows, daily risk
resets and position lifetime are separate. Define maintenance/opening gaps, carried
P&L, pending actions and expiry handling explicitly; do not assume session flattening
or automatic rolls. One feature catalogue combines indicators, volume, market
structure and support/resistance; exact causal definitions remain to be frozen in
FT-02. Later-confirmed pivots must never introduce lookahead. Detailed volume-at-price
and order-flow features require finer source data and remain outside this slice.

Provide both pause-new-entries/manage-exits and close-and-stop. Persist operator intent
across restarts. Automatically recover previously running paper lanes only after
fresh data and state verification; manual pauses remain in force. A close request
remains pending until eligible simulated execution; never fabricate offline fills.

Store validated, versioned strategy definitions as structured data. The first
editor uses forms for supported indicators, conditions, exits, sizing, sessions
and risk limits. Parameter changes and supported rule combinations need no code
changes or deployment. New primitives may require a reviewed code extension.

Do not execute arbitrary Python, shell code or unrestricted expressions submitted
through the UI or MCP. Avoid a general-purpose visual workflow engine initially.

Edits create a new version; running lanes stay pinned until explicitly changed.
Each lane owns its simulated account state, orders, fills and results. Backtests
and forward-paper runs use the same decision semantics and explicit fill models.
Internal paper lanes support many independent experiments; a broker paper account
can later validate integration, but cannot be assumed to isolate all virtual lanes.

Live execution later has one owning lane per broker account and contract initially.
Supporting multiple competing live lanes requires an explicit allocation/netting
policy and is deferred.

## Analysis and MCP

Capture the exact strategy version, data revision, engine version, fees, slippage,
fill assumptions and random seed when applicable for every run. Record signals,
indicator values and decision reasons at the time of the decision.

Report net returns, drawdown, trade counts, exposure, costs and trade-level evidence.
Support bounded parameter comparisons, unseen evaluation periods and sensitivity
analysis. Track all attempted variants to make selection bias visible. AI-generated
explanations are hypotheses grounded in records, not proof of causation.

Initial MCP operations inspect data coverage, create strategy drafts, validate
definitions, submit bounded backtests, retrieve/compare results and manage paper
lanes. Long jobs return a run ID. The server enforces ownership, resource budgets
and the same validation as the UI. Tool descriptions are not security controls.

The VPS runs saved strategies independently of a chat session. MCP does not itself
provide an always-on AI service. Live activation and risk-limit changes remain
explicit user operations against a specific version when live execution is added.

## VPS baseline and recovery

Netcup is being provisioned; its actual purchased configuration is not yet verified.
Use Docker Compose containers with a named PostgreSQL volume and persistent host
directories for per-user Parquet archives and reports. Give research workers read-only
archive access where possible. Application code belongs in versioned images; secrets
are supplied outside images and Git. Persistent mounts survive container replacement
but do not supply backups or high availability. Back up the database using database-aware
tools and preserve the corresponding archive/catalog revisions off the server.

Planning estimate: Linux LTS on x86-64, 4 vCPU, 16 GB RAM and 160-200 GB SSD/NVMe.
Consider 8 vCPU when running multiple concurrent backtests. Begin with one backtest
worker and cap its CPU/memory use. This is a provisional budget, not a benchmark.
Verify capacity with the intended number of broker sessions and representative runs.

Use HTTPS, restricted administrative access, encrypted credentials and encrypted
off-server backups. Test restoration, not only backup creation. Surface stale
feeds, failed jobs, unavailable broker sessions and disk pressure to operators.

On restart, recover persisted ingestion and lane state without duplicate decisions.
Missed historical events may repair research state but must not generate stale live
orders. Before live trading, reconcile broker state, handle uncertain submissions
without blind resubmission, enforce risk limits and provide an emergency stop.

## Broker decision still open

IBKR is the current cost-sensitive candidate, not a final selection. Prove account
eligibility, API entitlements, retention/automated-use permissions, contract lookup,
historical coverage, pacing, live subscriptions, multiple user sessions and recovery.

IBKR states that a fully headless TWS/IB Gateway session is unsupported. Its daily
auto-restart feature does not establish indefinite unattended authentication.
Validate a supported operational login/recovery workflow on the target VPS before
locking in deployment or promising unattended service.

Reference material checked during planning on 2026-09-12:

- [IB Gateway](https://www.interactivebrokers.com/docs/tws-api/doc/architecture/the-trader-workstation/the-ib-gateway)
- [IBKR market-data pricing and per-user rules](https://www.interactivebrokers.com/en/pricing/market-data-pricing.php)
- [GFIS agreement](https://ndcdyn.interactivebrokers.com/Universal/servlet/Registration_v2.formSampleView?formdb=3089)
- [Polars](https://docs.pola.rs/)
- [MCP authorisation](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)

## Deployment continuity

Seconds-level routine release interruption is an initial requirement. FT-18 implements
and measures replacement readiness, proxy cutover/drain, compatible migrations,
fenced worker handoff and rollback. Web-only updates keep ingestion/workers running.
See [release continuity](release-continuity.md) for provisional budgets and independent
HTTP/MCP, lane and ingestion metrics. Zero planned release downtime is a later target;
single-VPS host failures and broker reauthentication are separate availability concerns.
