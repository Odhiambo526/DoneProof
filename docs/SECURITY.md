# Security model

DoneProof is an assurance system. Its security objective is to prevent an execution agent's own success claim from being accepted as evidence of completion.

## Trust boundaries

A high-assurance run has four distinct actors:

1. **Requester / orchestrator** defines the desired outcome.
2. **DoneProof** registers the completion contract and trusted start time.
3. **Executor** performs the external action.
4. **Evidence provider** exposes the resulting state independently of the executor's textual claim.

The strongest deployment keeps evidence credentials and webhook signing secrets outside the executor's tool permissions.

## Temporal integrity

Use `POST /v1/runs` before execution. DoneProof overwrites caller-supplied timestamps with its own registration time. Discovery adapters then enforce that boundary.

`POST /v1/verify` remains available for imported or retrospective contracts but returns receipts with `assurance_level="submitted"`. Its caller-supplied temporal boundary is inherently weaker.

For mutable existing resources, `require_change=true` enables transition proof. DoneProof captures the predicate result before execution and signs both baseline and final minimal evidence into the receipt. If the desired state already existed before execution, the transition condition fails rather than crediting the agent.

## Safe uncertainty

DoneProof returns `UNKNOWN` rather than success when:

- a provider is inaccessible
- discovery matches multiple candidate resources
- GitHub's privacy-preserving 404 cannot distinguish missing from inaccessible state
- Gmail is not connected
- a provider response is malformed
- a verification request times out
- the contract lacks a usable adapter

## Provider constraints

### GitHub

Repository selectors are validated as `owner/repo`. Requests are restricted to `api.github.com`, redirects are disabled, and discovery is bounded by the registered task time.

### Gmail

The adapter only accesses the Gmail API. Access tokens are configuration secrets and are never stored in contracts or receipts. Evidence records expose normalized metadata, not message bodies.

### Webhooks

Webhook evidence uses HMAC-SHA256 over:

```text
timestamp.event_type.object_id.raw_json_body
```

The timestamp must be inside the configured replay window. Identical events are deduplicated by deterministic event ID. For strong independence, webhook secrets must not be available to the execution agent.

## Receipts

Receipts use Ed25519 signatures. The receipt hash is SHA-256 over the canonical signed payload. The public key ID is the first 16 hex characters of SHA-256 over the raw public key.

For production, configure a stable 32-byte signing seed through `DONEPROOF_SIGNING_SEED_B64`. Protect the seed using a secret manager. A KMS/HSM-backed signing provider is the recommended next control for regulated production environments.

## Tenant isolation

API keys map to tenant IDs. Contracts, receipts, idempotency keys and webhook evidence are scoped by tenant in persistence queries. Customer-facing APIs never query records without a tenant predicate when authentication is enabled.

## Secret handling

Keys with names matching token, secret, password, authorization, cookie, API key or credential patterns are redacted from evidence selectors before receipts are generated.

Do not put credentials into completion-contract selectors. Provider credentials belong in deployment configuration or a dedicated secret manager.

## Production deployment

The `/ready` endpoint reports unhealthy in production mode when API authentication or a stable signing key is missing.

Recommended controls outside the application process:

- TLS and WAF/API gateway
- secret manager/KMS
- managed PostgreSQL or equivalent durable database for larger deployments
- network egress policy
- centralized logs and metrics
- backup, retention and deletion policies
- per-tenant rate limits
- SSO/RBAC for the assurance console

## Known pilot limitations

The pilot persistence layer is SQLite. It is appropriate for evaluation, single-instance pilots and controlled workloads, but not the final storage architecture for horizontally scaled enterprise deployments.

Gmail authentication is currently configured through access tokens supplied to the deployment. A production multi-tenant OAuth connection manager is not included in this release.

The included assurance console is operationally useful but is not yet a full enterprise administration plane with SSO, RBAC, billing, policy authoring and key rotation workflows.
