# Release-candidate staging and release procedure

This is release hardening, not another product phase. Production is not authorized
for automatic deployment. An HTTP preview is not a running verification system.

## Staging topology

Select a small Linux VM running Docker Engine/Compose, with user namespaces and
the checked-in Chromium seccomp profile. This directly supports persistent Python
processes, non-root Chromium, PID/memory limits, graceful signals, independent
service scaling and bounded logs. No Kubernetes control plane is needed.
The platform selection is a deployment design; no VM was available to this task.

Use isolated Neon PostgreSQL 16 plus API, normal worker, browser worker and callback
receiver from `deploy/compose.staging.yml`. Set immutable image digests and
`DONEPROOF_REVISION` to the tested commit in each private environment file.
Do not use production database endpoints or production OAuth applications.
The API uses the browser-capable image because compiler preflight and transition
baseline capture run before external execution. Lightweight Vercel previews
cannot prove browser preparation availability.

Normal workers exclude jobs containing browser conditions. Browser workers
require such jobs and execute the whole mixed-provider contract. All processes
must have the identical provider registry and check revisions. Stop incompatible
old workers before enabling this routing; an old worker does not understand the
new deployment policy.

Bind API/callback ports to loopback and put a trusted TLS reverse proxy in front.
Do not enable access logs containing request headers, bodies, query strings or
OAuth callback codes at either proxy or platform. `logging.json` enables only
sanitized application events. No provider response bodies are log messages.
Do not expose the database, Docker socket, metrics SQL or inbox directly.

Private files must be readable only by the deployment operator and container
identity. Separate API/worker/browser/receiver files: the receiver needs only its
own inbox database credential and callback secrets; it receives no signer, OAuth
or customer API keys. API needs admin keys and OAuth start/callback settings.
Workers need scoped database, signer, token-vault and refresh configuration.
Chromium receives a reduced environment without these application secrets.
Use a managed secret store where the chosen host supports it; environment custody
is a pilot compromise, not a claim of HSM isolation.

Configure host egress controls for required DB/provider/model destinations and
approved browser targets, block metadata/private networks except the explicit DB
route, and validate those rules on the selected host. Browser transport already
pins validated public DNS addresses and verifies TLS, but host egress enforcement
has not been proven until staging is actually provisioned.

Deployment:

```sh
docker compose -f deploy/compose.staging.yml config --quiet
docker compose -f deploy/compose.staging.yml up -d
docker compose -f deploy/compose.staging.yml ps
docker compose -f deploy/compose.staging.yml logs --tail 100
```

Compose restart policies restart exited processes; an unhealthy status alone does
not restart a container. Alert and explicitly restart a stuck service. Worker
health measures freshness of all three local loops (verification/callback/recovery),
not a successful external-provider probe. A stale health file expires after
120 seconds and is removed on graceful shutdown. It contains no tenant evidence.
Graceful worker shutdown drains the current verification, callback and recovery
stage; it does not cancel a business verification job. This prevents an in-flight
database thread from acquiring an orphaned lease after shutdown. Use the job
cancellation API for cancellation semantics. A host kill after the grace period
still relies on durable lease expiry and fencing; exercise it in hosted staging.
`/ready` is uncached API readiness, checks the compatible schema, and explicitly
reports remote workers as unobserved. Do not treat it as the release gate.

Sources: [Compose production](https://docs.docker.com/compose/how-tos/production/),
[service health/resources](https://docs.docker.com/reference/compose-file/services/),
[secret handling](https://docs.docker.com/compose/how-tos/use-secrets/),
[Chromium sandbox](https://playwright.dev/python/docs/docker).

## Live gates and commands

Use controlled accounts/repositories/recipients and separate read-only OAuth
applications. The external test actor owns write/send credentials. DoneProof never
performs the repair. Perform GitHub installation + user OAuth and Gmail consent
in the trusted browser settings page; retain HttpOnly state/PKCE binding.
Record provider account IDs and connection revisions without recording tokens.
Exercise draft/incorrect state, signed failure, external correction, re-verification,
new linked receipt, pinned signature, disconnect/reconnect and stale disconnect replay.

```sh
python evaluations/run_compiler.py --mode live --release-candidate --output live-compiler.json
python scripts/rehearse_migration.py --expected-host "$RC_DIRECT_HOST" --output migration.json
```

The first command requires a configured Astra key and executes 175 reviewed tasks
where the deterministic path does not apply; it never forces maximum effort.
Provider worlds in that evaluation remain explicit fixtures. Separately test live
provider selector resolution and browser checks against staging. The extra browser
corpus cases test refusal without approved checks; they do not prove browser success.
The migration command requires explicit isolated-staging acknowledgement, a direct
branch DSN and a supplied historical public key; never substitute a production host.

Do not mark any live gate passed from fixtures, a preview URL, or a green unit suite.
Record unavailable/untested gates as release blockers.

## Observability and retention

`deploy/operations.sql` is an operator-only query set for queue depth/age, queue wait,
latency percentiles, retries, stale leases, verdicts/UNKNOWN, connection states,
callback backlog, artifact expiry, relation sizes and DB connections. Validate
queries against staging. DB CPU and process memory/utilization require host/Neon
telemetry; no synthetic substitute is supplied.

API JSON events expose route templates, status, duration and a generated trace ID.
Compiler events expose fixed status, duration, call/escalation counts, never task
text. Worker events expose generated job IDs, stage and duration. Assurance audit
events connect trace IDs to sessions/jobs; receipt/outbox/inbox IDs finish the
correlation chain. Metric labels must be bounded categories, not customer identifiers.
Collect API rate/error counts from these events including reverse-proxy rejection
counts; per-process logs are not globally aggregated counters.

Retention:

- Receipts, registered contracts/baselines, immutable chains, sessions and assurance
  audit records: retain; no automatic deletion is introduced.
- Operational attempts/outbox: retain through investigations; define a reviewed
  archival policy before sustained scale. Six callback attempts within a 24-hour
  horizon are at-least-once, with durable inbox deduplication.
- Inbox: retain stable event IDs and typed payloads beyond the replay horizon.
  Acknowledgement means durable receipt, not business success or downstream processing.
  A separate consumer must track its own processing transaction.
- Screenshots: existing encrypted seven-day expiry and 512-per-tenant cap,
  maximum 64 KiB each. Run `python scripts/browser_artifacts.py purge-expired`
  on a scheduled operator job, including after periods without observations.
- OAuth metadata: retain identity/revision history; revoke and clear credentials
  through supported lifecycle operations. Preserve old encryption keys through
  credential/artifact re-encryption or expiry.

At the screenshot cap, plaintext is at most 32 MiB per tenant; base64/encryption/JSON
and indexes add overhead. Estimate assurance growth from measured relation-size
deltas per workflow, multiplied by expected daily volume and retention. Do not
delete receipts to meet a storage quota. The current Neon account's configured
restore history is six hours: confirm a separate backup/recovery plan before release.

## Signing-key rotation

1. Inventory the current key_id/public key and archive its independently trusted
   pin. Preserve encrypted backups of the old seed under restricted custody.
2. Stop new job admission and drain all QUEUED/OBSERVING/EVALUATING/SIGNING work,
   including recovery. Confirm callback backlog is understood.
3. Generate the next Ed25519 seed in the approved secret store. Never print it,
   put it in a build argument, or submit it to a diagnostic endpoint.
4. Deploy the same new signer to API and both worker classes. Publish the new
   public pin through an independent channel before new receipt consumption.
5. Verify a new controlled receipt with the new pin and old receipts with the
   archived old pin. Never rewrite or re-sign historical receipts.
6. If an old SIGNING job survives, it fails closed with signing_key_changed.
   Do not relabel it complete or silently substitute the new key. Use explicit
   recovery/registered-boundary procedures.

Current tests cover signing transaction rollback, key mismatch rejection and
schemas 1.0/1.1/1.2 verification. Live production-seed rotation was not performed.
External Ed25519 HSM/KMS signing can be a GA follow-up; choose a service supporting
the existing exact-byte signature contract before changing custody.

## Release and rollback

1. Confirm tested commit/image digests, database recovery and independently archived
   signer pins. Stop if any report blocker remains.
2. Disable admission; snapshot and rehearse schema 1→7 in isolation. Apply additive
   migrations with the direct DSN. Interruption must roll back and retry cleanly.
3. Deploy compatible API with managed features still restricted to internal tenants.
   Its readiness cannot approve remote worker availability.
4. Stop/drain old workers; deploy both routed worker classes and the callback inbox.
   Confirm registry/check/signer versions and local health.
5. Enable controlled OAuth and compiler configuration, complete live negative and
   positive assurance/repair flows, callback replay tests and browser classification.
6. Complete crash-stage tests and a measured load/soak window. Inspect stale leases,
   callback backlog, memory, DB connections and artifact growth.
7. Only after explicit human promotion run production smoke, then progressively
   enable design-partner tenants.

Before schema commit, roll back the transaction. After additive migrations, retain
the schema; no destructive down migration exists. Before new work is admitted,
an application rollback may restore the prior tested API after compatibility review.
After sessions/jobs exist, do not blindly roll back to main: stop ingress/workers,
preserve the database and prefer a compatible forward fix. Snapshot restoration
would discard post-snapshot receipts/jobs and is not an acceptable routine rollback.
Provider/check or signer changes require draining or explicit fail-closed job recovery.
