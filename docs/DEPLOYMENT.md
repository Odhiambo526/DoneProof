# Pilot deployment

This release supports durable PostgreSQL persistence for production and SQLite for local development.

## Minimum production-like configuration

Set:

```text
DONEPROOF_ENV=production
DONEPROOF_API_KEYS_JSON=...
DONEPROOF_SIGNING_SEED_B64=...
DATABASE_URL=postgresql://user:password@host/database?sslmode=require
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
    ┌──────────┼─────────────┐
    ▼          ▼             ▼
PostgreSQL   Provider      Secret
 durable     APIs          manager
 store
```

For a real customer pilot:

1. terminate TLS before DoneProof
2. restrict egress to required provider APIs
3. keep execution-agent credentials separate from evidence credentials
4. use a managed PostgreSQL database with TLS enabled
5. enable provider backups / point-in-time recovery according to pilot retention requirements
6. store API keys, provider tokens and signing seed outside the repository
7. monitor `/health` and `/ready`
8. export evidence bundles for materially important outcomes

## Readiness endpoint

```text
GET /ready
```

Production mode fails fast at startup if workspace authentication, a stable signing key, or durable PostgreSQL storage is missing. `/ready` reports the active storage backend and whether it is durable.

## Scaling beyond the pilot

Before horizontal multi-instance production, replace or externalize:

- in-process rate limiter → API gateway/Redis-backed limits
- static access tokens → managed OAuth/credential lifecycle
- process-local signing seed → KMS/HSM signer
- API-key console → SSO/RBAC administration plane

These changes do not alter the completion-contract or receipt model.

## Vercel + managed PostgreSQL

DoneProof declares `doneproof.app:app` as its Vercel FastAPI entrypoint and pins Python 3.12. No `vercel.json` rewrite is required.

For durable Vercel deployments, provision a managed PostgreSQL provider and expose its connection string as `DATABASE_URL`. The Vercel Marketplace currently offers native PostgreSQL providers such as Neon. DoneProof automatically selects PostgreSQL whenever `DATABASE_URL` (or `POSTGRES_URL`) is present.

Recommended production settings:

```text
DONEPROOF_ENV=production
DATABASE_URL=postgresql://...
DONEPROOF_API_KEYS_JSON=...
DONEPROOF_SIGNING_SEED_B64=...
```

If Vercel is running without `DATABASE_URL`, DoneProof retains `/tmp/doneproof.db` only as a development/deployment-smoke fallback. **Production mode refuses to start on that ephemeral store.**

The PostgreSQL schema is created idempotently at startup under a PostgreSQL advisory lock so concurrent serverless cold starts cannot race the initial schema bootstrap.
