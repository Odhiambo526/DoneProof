# Release-risk map before hardening

Inspected base: 100cdb7826bd289ed9dd548f2d1fcf7880305eba (Phase 7, PR #11).
All phase PRs remained drafts. Production Neon reported migration 1 on 2026-09-06.

| Severity | Evidence | Release consequence |
| --- | --- | --- |
| BLOCKER | No accessible persistent staging worker host, controlled OAuth accounts, or model key | Live end-to-end gates cannot be approved |
| HIGH | Production schema 1; stack schema 7 | Rehearse full upgrade, interruption and signed fixture preservation |
| HIGH | Job claim has no worker-capability partition | Incapable workers can consume browser jobs and return avoidable UNKNOWN |
| HIGH | Synchronous queue I/O in asyncio worker; repeated local concurrency timing failure | DB contention can spend provider deadlines; preserve limits while isolating blocking I/O |
| HIGH | Provider/model/OAuth response checks occur after buffering or are absent | Bound response bytes before parsing; reject unsupported compression |
| MEDIUM | Ready response is cacheable and tests only DB connectivity | Separate uncached API readiness from unobserved system health |
| MEDIUM | No container-local worker loop health or durable callback receiver deployment | Add operational checks and a durable signed receiver; do not claim remote liveness |
| MEDIUM | Settings repr and provider error IDs can expose sensitive configuration/caller text | Suppress secret-bearing repr fields and untrusted identifiers in logs |
| MEDIUM | Rotation, rollback and retention procedures are incomplete | Produce exact runbooks and retain immutable assurance records |
| LOW | Two pre-existing dependency deprecation warnings | Track separately; do not weaken tests to silence them |

Audit traced API/compiler/resolver/session/run/baseline/job/worker/observation/
evaluation/signing/callback/recovery/console/SDK paths, all migration modules,
provider registry/plugins, managed OAuth/PKCE/encryption, browser sandbox/network/
artifacts, readiness, Vercel and Docker configuration. This is a release audit,
not a claim of exhaustive proof or live provider validation.
