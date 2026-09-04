# Security and assurance model

DoneProof's primary security objective is simple:

> **An execution agent's own success claim must never be sufficient evidence of completion.**

## Trust boundaries

A high-assurance run separates four roles:

1. **Requester/orchestrator** — defines the intended business outcome.
2. **DoneProof** — registers the contract, timing boundary and optional baseline.
3. **Executor** — performs the action.
4. **Evidence provider** — exposes authoritative resulting state independently of the executor's textual claim.

The strongest deployment keeps evidence credentials and webhook signing secrets outside the executor's tool permissions.

## Temporal and transition integrity

Use `POST /v1/runs` before execution. DoneProof assigns the run ID and server timestamp.

For new-resource discovery, provider searches are bounded by that timestamp.

For updates to an existing resource, `require_change=true` captures the pre-execution predicate status. A desired state that was already true before execution does not count as a verified transition.

## Safe uncertainty

DoneProof returns `UNKNOWN`, not success, when authoritative state cannot be established safely. Typical causes include:

- provider access failure
- ambiguous resource discovery
- privacy-preserving provider responses that cannot distinguish missing from inaccessible state
- missing credentials
- malformed provider responses
- timeout
- unsupported/unresolved evidence paths

## Provider constraints

### GitHub

Repository selectors are validated as `owner/repo`. Requests are constrained to `api.github.com`, redirects are disabled, and discovery is bounded by the run start time.

### Gmail

The adapter accesses the Gmail API only. Access tokens live in deployment configuration, not contracts or receipts. Normalized evidence excludes message bodies.

### Trusted webhooks

Webhook evidence uses HMAC-SHA256 over:

```text
timestamp.event_type.object_id.raw_json_body
```

Events outside the replay window are rejected and accepted events receive deterministic event IDs. For strong independence, the webhook secret must not be accessible to the executor.

## Receipt integrity

Receipts are Ed25519-signed and include both an exact completion-contract hash and the public key used for signing.

Historical receipts verify against their embedded public key after key rotation. Production deployments should still manage trust and rotation using a secret manager and, for regulated environments, KMS/HSM-backed signing.

## Tenant isolation and immutability

API keys map to workspace tenant IDs. Contracts, receipts, baselines, idempotency records, audit events and evidence lookup are tenant-scoped.

A completion-contract ID cannot be silently reused for different content. This prevents a receipt reference from becoming ambiguous later.

## Secret handling

Known credential-like selector keys are redacted before evidence is placed in receipts. Credentials should never be placed inside completion contracts in the first place; provider credentials belong in deployment configuration or a dedicated secret manager.

## Browser console

The pilot console accepts a workspace API key for convenience but keeps it in browser session storage only. For a managed enterprise console, replace API-key entry with organization login, SSO and RBAC.

## Production perimeter controls

Recommended controls outside the application process:

- TLS and API gateway/WAF
- service identity and secret manager
- network egress policy
- centralized logs and metrics
- durable managed database and backups
- retention/deletion policy
- distributed rate limiting
- SSO/RBAC for human administration

## Current pilot limitations

- SQLite is a local-development fallback only. Production mode requires durable PostgreSQL via `DATABASE_URL`.
- Gmail uses deployment-supplied access tokens; managed multi-tenant OAuth is not included.
- Signing is process-local Ed25519 from a configured seed; KMS/HSM integration is not included.
- The console is an operational pilot surface rather than a full administration plane.
- DoneProof establishes observed state/transition, not causal attribution to a specific actor unless the underlying evidence itself contains trusted actor identity.
