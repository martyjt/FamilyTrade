# Release continuity plan

Status: planning only. The user requires seconds-level deployment interruption
initially and zero planned deployment downtime in the medium to long term.
No uptime result has been measured and no deployment is authorised here.

The numerical budgets, release classes, probe windows and ownership/rollback
semantics are now specified in contracts-v1.md section 12. After its final review
gate, those defaults supersede the provisional numbers/pending-agreement text
below. They are acceptance targets to measure in FT-18, not observed availability.

## Initial release boundary

Proposed budget: at most five seconds of contiguous public API/UI unavailability
during a routine application cutover. The exact number awaits user agreement in
FT-02. This is an interruption budget, not total build or deployment duration.
Measure MCP connection recovery and lane processing gaps independently; FT-02 must
set their budgets before implementation. Minute candles do not make processing
delays harmless by definition.

Routine releases keep the host, Docker daemon, proxy, database and provider gateway
running. Web/API-only releases also leave ingestion and paper workers running.
An application release must not incidentally recreate persistent services, shared
networks or volumes. Any reconnect caused by the release counts against that
release, even when provider recovery is outside FamilyTrade's control.

Use prebuilt, versioned images. Start a replacement API beside the existing API
under a distinct endpoint; functional readiness checks cover compatible schema,
authentication and required dependencies. Switch proxy routing, stop admitting new
requests to the old process and drain it with a bounded timeout. Keep its image and
configuration available for rollback. Failed readiness means no cutover; failed
post-cutover probes require a tested switch-back while schemas remain compatible.

Use expand/contract database changes: old and new versions must work concurrently;
backfills have separate resource/lock budgets; destructive contraction waits until
the old version and rollback window are gone. Locking rewrites and incompatible
migrations are separately planned maintenance, not disguised as routine releases.

Worker upgrades use the FT-08/FT-11 durable ownership and checkpoint protocol.
A monotonically increasing ownership token must fence stale writes, with atomic
state/decision/fill checkpoints and idempotent replay. Specify lease timing, crash
takeover and graceful relinquishment. Never run two valid owners for one lane or
user feed. No unqualified exactly-once delivery claim. A web-only release must not
exercise this handoff at all.

Persist operator intent independently from recovery status. Paused lanes remain
paused after deployment. Close-and-stop requests remain pending when a valid paper
fill cannot occur; do not fabricate closure during missing data or a closed market.

## Evidence required

- Continuous public-path HTTP probes before, during and after cutover; freeze the
  probe rate/window and acceptance thresholds in FT-02. Report failed requests,
  maximum contiguous outage and p95/p99 latency, not only container health.
- Authenticated MCP and any WebSocket/SSE probes: connection drops, reconnect/resume
  time, safe handling of in-flight writes and retry idempotency.
- Per-lane last processed event/checkpoint, maximum processing gap and ingestion
  lag; no missing or duplicate decisions/fills and no ownership overlap.
- Failed readiness, failed post-switch health, rollback, graceful worker handoff
  and forced-owner-loss cases, including an open overnight position and a paused
  lane. Test the actual authorised VPS; local fixtures are preparation only.
- A release evidence artifact records revisions, release class, observation window,
  results, rollback boundary and independent provider/auth failures. Separately
  report host/database/gateway maintenance and unplanned external outages.

## Medium- to long-term target

Zero user-visible request failures for routine application releases, with no lost
or duplicate persisted effects and processing lag within an agreed budget. Build
on overlapping ready instances, compatible schemas, worker handoff and resumable
client connections; prove it with the same external probes. A temporarily buffered
event is not lost, but its processing delay still counts and must be reported.

This target is distinct from resilience to VPS, disk, Docker daemon or network
failure. A single VPS cannot guarantee service through its own failure/reboot.
Multi-host failover is a separate future decision if that availability is required.
Kubernetes and additional servers are not prerequisites for the initial design.

## References checked during planning

- [Docker Compose production updates](https://docs.docker.com/compose/how-tos/production/):
  targeted service recreation avoids dependencies but still recreates that service.
- [Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy):
  reload can close upgraded connections by default; explicit drain/recovery matters.
- [PostgreSQL ALTER TABLE](https://www.postgresql.org/docs/current/sql-altertable.html):
  schema changes can take restrictive locks; migration continuity requires evidence.
