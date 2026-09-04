# Pilot deployment

This release is designed for a controlled single-instance deployment.

## Minimum production-like configuration

Set:

```text
DONEPROOF_ENV=production
DONEPROOF_API_KEYS_JSON=...
DONEPROOF_SIGNING_SEED_B64=...
DONEPROOF_DB=/data/doneproof.db
```

Add only the provider credentials required for the pilot.

## Recommended perimeter

```text
Internet / internal network
          │
          ▼
TLS + API gateway / WAF
          │
          ▼
DoneProof application
          │
    ┌─────┼─────────────┐
    ▼     ▼             ▼
 SQLite  Provider      Secret
 pilot   APIs          manager
 store
```

For a real customer pilot:

1. terminate TLS before DoneProof
2. restrict egress to required provider APIs
3. keep execution-agent credentials separate from evidence credentials
4. persist `/data`
5. back up the database according to pilot retention requirements
6. store API keys, provider tokens and signing seed outside the repository
7. monitor `/health` and `/ready`
8. export evidence bundles for materially important outcomes

## Readiness endpoint

```text
GET /ready
```

Production mode fails fast at startup if workspace authentication or a stable signing key is missing. `/ready` then reports database/runtime readiness for a valid deployment.

## Scaling beyond the pilot

Before horizontal multi-instance production, replace or externalize:

- SQLite → PostgreSQL or equivalent managed durable store
- in-process rate limiter → API gateway/Redis-backed limits
- static access tokens → managed OAuth/credential lifecycle
- process-local signing seed → KMS/HSM signer
- API-key console → SSO/RBAC administration plane

These changes do not alter the completion-contract or receipt model.
