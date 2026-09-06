# DoneProof release-candidate report

## Identity and decision

Exact stacked base: 100cdb7826bd289ed9dd548f2d1fcf7880305eba, Phase 7 PR #11.
Branch: codex/release-candidate-hardening. This is release integration/hardening,
not a new feature phase. Tested implementation commit:
`1ab6f3f017c394688801350b7cce2d88ca31c08d`.
[Draft PR #12](https://github.com/Odhiambo526/DoneProof/pull/12) remains based on
Phase 7; the following report/runbook commit changes documentation only.

No merge or production deployment was performed. The exact stack cannot yet be
recommended for production: required hosted staging and live-provider gates have
not been demonstrated.

## Audit and fixed issues

The full lifecycle and migration modules were inspected before code changes;
see RC_RISK_MAP.md. Fixes address unfiltered heterogeneous worker queues,
synchronous worker database waits consuming provider deadlines, unbounded
provider/model/OAuth response reads, chunked request bounds, secret-bearing
configuration repr, legacy validation responses reflecting invalid secret-bearing
input, caller-controlled error-log identifiers, readiness caching,
future-schema startup refusal and operational visibility.

Normal/browser workers now partition complete jobs. Queue database work runs
off the event loop. Provider response bodies are limited before parsing and
unsupported compression is refused. No predicate, verdict, receipt signature,
browser assurance classification or API-first policy is relaxed.

Benchmark review found a further worker shutdown race: cancelling a database
thread's await did not cancel its transaction, allowing a late claim to remain
leased for 90 seconds. Graceful shutdown now drains the current stage. A regression
test cancels a blocked claim and proves immediate completion without moving the
clock or waiting for lease expiry. Initial SDK benchmark mean latency was 9.422 s
with one 28-poll job; after the fix it is 216.488 ms with two polls for every job.
This is graceful process shutdown, not a change to job cancellation semantics.

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

Real public GitHub REST observations, stored as durable jobs in the isolated Neon
branch, independently checked PR #12: `open` returned VERIFIED and `closed`
returned FAILED. Both new receipts verified with a pinned, ephemeral test public
key. This finite local worker harness used no OAuth token, mocks or external
business writes. It is not proof of managed OAuth or persistent hosted workers.
The temporary local database credential file was removed after the rehearsal.
The isolated branch is retained for review; production data was not changed.

## Validation results

Implementation CI: [push run 34023971204](https://github.com/Odhiambo526/DoneProof/actions/runs/34023971204)
and [PR run 34023973641](https://github.com/Odhiambo526/DoneProof/actions/runs/34023973641).
Counts below are read from completed job logs, not inferred from collection.

| Gate | Result |
| --- | --- |
| Ruff and generated SDK/schema/provider checks | Passed |
| Python 3.11, 3.12, 3.13 | Each 502 passed, 262 expected skips |
| PostgreSQL 16 full suite | 746 passed, 18 expected Chromium skips |
| Real Chromium suite | 18 passed; covers the separate browser gate |
| Local Python 3.12 with Chromium | 520 passed, 244 expected PostgreSQL skips |
| TypeScript strict lint/typecheck/tests | Passed; 18 tests, zero failures/skips |
| API/durable worker image | Built; worker start and graceful exit code 0 |
| Browser image | Built; non-root sandbox smoke, bounded screenshot and cleanup passed |
| Python and TypeScript integration demos | Passed with explicit controlled fixtures and pinned signatures |
| Vercel preview | Success for the implementation commit |
| Production smoke | Intentionally gated and skipped |

No unexpected skips. Two pre-existing dependency deprecation warnings remain.
Migration coverage includes 42 populated schema/interruption combinations across
SQLite and PostgreSQL plus two schema compatibility checks. Receipt schemas
1.0/1.1/1.2 retain their bytes and pinned signatures. This does not prove rolling
deployment of every historical API/worker binary against new live traffic.

The process test uses controlled signed webhook events and actual API/worker
processes; it is not live Gmail/GitHub OAuth or hosted staging evidence.
Existing bounded retry, cancellation/signing, lease fencing, callback replay,
browser instability and tenant-isolation regression suites remain enabled.

## Compiler and performance evidence

The original 125-task corpus retains 79 valid contracts, zero false-certifiable
contracts, zero unnecessary UNKNOWN and 46 clarifications. The expanded 175-task
corpus has 107 valid contracts (61.14%), 68 clarifications (38.86%), 69/77 selector
resolution checks successful (89.61%), and 66 intentionally deferred selectors.
It checks 244 negative fixture worlds with zero false-certifiable outcomes and
zero unnecessary UNKNOWN. Compilation p50/p95 is 1.552/2.684 ms. Model calls and
tokens are zero, so model escalation rate is undefined and token cost is $0.
These are offline correctness results, not live Astra evaluation. The extra
50 tasks include 30 paired-provider tasks, ten ambiguous tasks and ten browser
refusal tasks; approved-browser positive workflows remain a separate live gate.

Ten-repeat orchestration p50 timings in milliseconds, synthetic providers:

| Conditions | Phase 7 sync | RC sync | Phase 7 PostgreSQL durable | RC PostgreSQL durable | RC durable with 5 ms provider delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.453 | 0.307 | 235.597 | 164.580 | 174.471 |
| 10 | 1.713 | 1.155 | 267.492 | 187.109 | 194.505 |
| 100 | 14.368 | 9.493 | 1007.372 | 698.149 | 752.389 |
| 1000 | 145.519 | 95.298 | 9176.854 | 6583.804 | 6836.586 |

The unchanged legacy engine benchmark also ran faster on this RC runner
(0.208/0.628/4.809/49.146 ms), so these differences must not be attributed solely
to code improvements. No material regression is demonstrated. Durable timings
include job creation, leases, checkpoints, signing and receipt reads; sync timings
exclude database persistence. Provider concurrency remains bounded at 16.
The historical 100/1000-condition baseline bypasses its old API validation limit.

SDK fixture results: prepare mean 17.390 ms; end-to-end mean 216.488 ms;
two polling requests for every one of ten jobs; Python no-network SDK overhead
0.329 ms and serialization/validation 0.0456 ms; TypeScript 0.323 ms and
0.0351 ms respectively. Two declaration requests use one HTTP call; two live
capability requests still use two. The SDK benchmark uses in-process ASGI/mock
fetch, not a deployed network service. Phase 7 end-to-end mean was 223.208 ms.

Controlled saturation: 1000 queued jobs, four local SQLite workers, 1000 completed
receipts, zero stale provider slots, peak provider concurrency three, elapsed
14.978 s. Queue wait mean 4.079 s; creation-to-completion p50/p95/p99
12.267/12.646/12.753 s. This is a bounded fixture load test, not a multi-provider
production capacity claim or a multi-hour soak. CPU/memory/DB utilization under
real staging load remains unmeasured.

## Security and chaos evidence

Permanent tests cover cross-tenant session/receipt/recovery access, duplicate
preparation and lost HTTP mutation responses, stale provider declarations,
disconnect/refresh/observation races, callback signature/timestamp/replay binding,
executor observations/screenshots/guidance rejection, browser API-first refusal,
DNS/private-network/redirect restrictions, receipt downgrade/lineage and
cancellation/signing fencing. New tests cover bounded chunked/provider responses,
secret repr/validation/log reflection, queue partitioning, readiness failures,
durable receiver deduplication after restart, and worker shutdown during claim.

A real loopback HTTP/SDK/separate-worker test observes failure, kills the worker,
queues independent repair verification, restarts and obtains a new linked VERIFIED
receipt. Migration interruption uses transactional exception injection after each
migration. Existing publication rollback and stale-lease tests remain enabled.
These controlled tests do not replace hosted kill/network/browser fault injection
at every requested stage. No false VERIFIED or signature/tenant invariant failure
was observed in the completed tests. This is bounded evidence, not exhaustive proof.

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
