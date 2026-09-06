# DoneProof release-candidate report

## Identity and decision

Exact stacked base: 100cdb7826bd289ed9dd548f2d1fcf7880305eba, Phase 7 PR #11.
Branch: codex/release-candidate-hardening. This is release integration/hardening,
not a new feature phase. The implementation commit and final CI evidence are
recorded in the draft PR; this initial report will be finalized after CI.

No merge or production deployment was performed. The exact stack cannot yet be
recommended for production: required hosted staging and live-provider gates have
not been demonstrated.

## Audit and fixed issues

The full lifecycle and migration modules were inspected before code changes;
see RC_RISK_MAP.md. Fixes address unfiltered heterogeneous worker queues,
synchronous worker database waits consuming provider deadlines, unbounded
provider/model/OAuth response reads, chunked request bounds, secret-bearing
configuration repr, caller-controlled error-log identifiers, readiness caching,
future-schema startup refusal and operational visibility.

Normal/browser workers now partition complete jobs. Queue database work runs
off the event loop. Provider response bodies are limited before parsing and
unsupported compression is refused. No predicate, verdict, receipt signature,
browser assurance classification or API-first policy is relaxed.

The callback receiver persists authenticated events atomically in PostgreSQL
and acknowledges duplicates. Delivery is at-least-once, not exactly-once.
Production consumption must track its own durable processing state.

## Staging topology and migration evidence

Actual provisioned infrastructure: isolated Neon PostgreSQL 16 branch
`br-round-waterfall-a5sbvmhu`, forked from production branch
`br-round-fog-a5aexvfr` in project `summer-cake-41826008`.
The fork was upgraded from schema 1 through 7. Original audit row hashes matched.
The snapshot had zero contracts, receipts, baselines or events and one audit row;
therefore this rehearsal does not establish historical receipt continuity.
Populated automated fixtures separately cover signed schemas 1.0/1.1/1.2, all six
migration interruption boundaries, restart/idempotence and future-schema refusal.
The schema-1 Store fixture is captured from repository commit
`d305af31da52ca3c567784f35615c9c203863ef2`; matching the observed schema is not proof
that this exact commit is the Vercel production deployment.

The current production public signing-key endpoint returned key ID
`cf8ea856bb4e6a53`. No signing seed was retrieved, rotated or deployed. No customer
evidence was printed. Production schema inspection was read-only.

Prepared topology: a Linux Docker/Compose host, isolated Neon, API with browser
preflight capability, ordinary worker, separate sandboxed browser worker,
durable callback receiver and trusted TLS proxy. No staging VM was accessible;
the topology was not deployed. Vercel preview covers HTTP build/startup only.
The local Docker daemon could not start, so image validation is assigned to CI.

## Validation checkpoint

- Local complete Python suite with real Chromium: 518 passed, 243 PostgreSQL skips,
  two existing dependency deprecation warnings.
- Targeted worker suite: 53 passed, 38 PostgreSQL skips. The unchanged concurrency
  assertion passed after removing blocking queue I/O from the event loop.
- Populated migration fixture suite: 22 passed locally, 22 PostgreSQL cases for CI.
- Real loopback HTTP + Python SDK + separate worker-process restart + signed
  controlled webhook failure/repair/receipt-chain test passed locally.
- Expanded offline compiler corpus: 175 tasks, 107 valid, zero false-certifiable,
  zero unnecessary UNKNOWN. No live model calls or paid token use.
- Final Python matrix, PostgreSQL 16, TypeScript, Docker/browser sandbox,
  queue saturation, orchestration benchmarks and Vercel preview: CI pending.

The process test uses controlled signed webhook events and actual API/worker
processes; it is not live Gmail/GitHub OAuth or hosted staging evidence.
Existing bounded retry, cancellation/signing, lease fencing, callback replay,
browser instability and tenant-isolation regression suites remain enabled.

## Live and operational gates

Not performed: managed GitHub/Gmail OAuth with controlled accounts; live model
175-task evaluation; hosted browser targets; public staging callback delivery;
live Python and TypeScript SDK provider flows; host network/seccomp validation;
fault injection at every requested process/browser stage; 100 concurrent mixed
assurance sessions; real mixed-provider load; multi-hour soak; DB CPU and
API/worker/browser memory utilization under that load.

No worker host, controlled account/recipient identifiers or secure OAuth/model
configuration location was supplied. The Vercel connector returned no teams.
These are unavailable gates, not passing results.

## Signing, operations, retention and rollback

RC_OPERATIONS.md specifies secret separation, exact image/version visibility,
local worker health versus API readiness, operator metric queries, callback
guarantees, retention, key rotation and staged rollout/rollback.
Immutable receipts and lineage are never cleaned up. Browser artifacts retain
the existing encrypted seven-day/512-per-tenant bounds. Operational record
archival and capacity must be established before sustained pilot load.

Environment-secret signing custody remains; external KMS/HSM is a GA option,
not implemented here. Rotation drains outstanding jobs, archives old public pins
and verifies old receipts without rewriting them. A key mismatch at SIGNING
fails closed. Actual production key rotation was not performed.

Migrations are additive. After new sessions/jobs are admitted, rollback to main
is not automatically safe: stop ingress/workers and prefer a compatible forward
fix. Restoring a pre-release snapshot can discard newer assurance records and is
not a routine rollback. The Neon project's observed recovery history was six
hours; confirm backup and restore capability before any release.

## Release blockers

1. Provision and validate the persistent staging host and browser egress/sandbox.
2. Complete controlled GitHub/Gmail OAuth and authoritative negative/positive flows.
3. Run the live model corpus with zero false-certifiable contracts.
4. Complete hosted SDK/callback, every required crash-stage and mixed-load/soak gates.
5. Confirm production deployment identity, secret custody, pinned-key continuity
   and tested recovery/rollback operations.

Green automated tests cannot resolve these missing operational proofs.

NOT READY FOR PRODUCTION
