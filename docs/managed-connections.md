# Managed connections (Phase 1)

DoneProof uses one Gmail mailbox and one GitHub user account per workspace (the existing tenant ID). GitHub access is further limited by the GitHub App's selected repository installations and the user's own access. These connections supply credentials to the existing authoritative adapters; they do not supply evidence. Executor statements, submitted credentials and arbitrary verification URLs are never accepted as proof.

## Deployment setup

Apply this change as a normal application deployment after provisioning:

- `DONEPROOF_PUBLIC_URL`: the exact public HTTPS origin, with no path/query. Only local development permits HTTP on localhost or 127.0.0.1. Never derive this value from a request Host or forwarding header.
- `DONEPROOF_CONNECTION_ADMIN_KEYS_JSON`: JSON mapping independent, random operator keys to existing tenant IDs. Do not give these keys to executors. Reusing any verification API key is rejected. All connection APIs except the browser callback require an administrator key in `X-DoneProof-Key`; ordinary verification keys retain their existing permissions.
- `DONEPROOF_CONNECTION_ENCRYPTION_KEYS_JSON`: JSON mapping stable key IDs to base64-encoded 32-byte random keys, and `DONEPROOF_CONNECTION_ACTIVE_KEY` selecting the current write key. Generate keys in a secret manager. Keep them separate from receipt-signing keys and database backups; supply the same key ring to every instance.
- Gmail: `DONEPROOF_GOOGLE_CLIENT_ID`, `DONEPROOF_GOOGLE_CLIENT_SECRET`. Enable the Gmail API, configure a Web Application OAuth client and register exactly `<origin>/v1/connections/oauth/gmail/callback`. Request only Gmail readonly; offline access and consent are requested by DoneProof. Configure the consent screen and complete Google's applicable verification requirements before general partner rollout. Google testing-mode grants can expire sooner.
- GitHub: create a GitHub App with **Issues: read**, **Pull requests: read**, and **Metadata: read**, no write permissions. Enable expiring user access tokens, set the callback exactly to `<origin>/v1/connections/oauth/github/callback`, and configure `DONEPROOF_GITHUB_CLIENT_ID`, `DONEPROOF_GITHUB_CLIENT_SECRET`, `DONEPROOF_GITHUB_APP_SLUG`. The partner installs the app on selected repositories from Connection Settings, then authorizes their account. OAuth Apps with broad `repo` write access are intentionally not used.

No provider accounts, secrets or production settings are created by the migration. A missing onboarding configuration disables Connect; normal receipt and public verification APIs remain available. Malformed encryption configuration fails startup. Missing historical decryption keys make affected connections unavailable.

Open **Assurance console → Connection Settings**, enter the workspace's connection administrator key and connect the provider. The key is kept only in page memory, cleared on navigation, and never passed to the provider. Re-enter it after OAuth redirects back. Connection labels and messages are inserted as text. The settings page has a strict script policy and is never cached.

## API contract

All paths below start with `/v1/connections`. New response schemas are published in `/docs`. IDs are opaque; the server derives tenant ownership from the administrator key.

| Method and path | Behavior |
| --- | --- |
| GET `/` (without trailing slash preferred) | Workspace connections and provider onboarding availability |
| GET `/{id}` | Safe connection metadata |
| POST `/{gmail\|github}/authorize` | Authorization URL and an HttpOnly browser-binding cookie |
| GET `/oauth/{gmail\|github}/callback` | Single-use callback; redirects to Settings with a fixed success/failure fragment |
| POST `/{id}/health` | Refresh if needed, check provider identity and read permissions, update state |
| POST `/{id}/disconnect` | Disable immediately, invalidate pending OAuth and revoke; retry this action if revocation remains pending |
| POST `/{id}/confirm-external-revocation` | After the operator revokes access in provider settings, erase retained credentials; connection remains disabled |
| POST `/{id}/rotate-key` | Re-encrypt stored connection credentials using the active key |

There are no token-upload, arbitrary endpoint, caller-controlled tenant or redirect URL parameters. Foreign/missing IDs return the same 404. Unauthorized connection management returns 401; a conflicting operation or pending revocation returns 409. Cross-origin browser administration is rejected. Connection state is one of `connected`, `expired`, `reconnect_required`, `disabled`, `error`. Timestamps are Unix seconds. Responses expose account label, approved scopes, expirations, last health check, safe error identifier and pending-revocation flag; never credentials, ciphertext, PKCE verifier or OAuth code.

Capabilities keep the existing `available/configuration_required/disabled` vocabulary. Gmail and authenticated GitHub capabilities reflect persisted state, expiration and decryption availability. Existing public GitHub reads remain anonymous when no connection exists. A disabled or broken managed GitHub connection never falls back to a configured/global token or anonymous evidence.

## Lifecycle and security boundaries

Credentials and PKCE verifiers use versioned AES-256-GCM envelopes with fresh nonces and authenticated tenant/provider/connection/purpose context. Copying ciphertext across tenants or rows fails authentication. OAuth state and browser proofs are stored only as hashes; state expires after ten minutes and is consumed atomically. PKCE S256, fixed callbacks, provider-specific state and browser binding prevent callback replay or account linking in another browser. Starting a newer authorization supersedes the older one.

Access tokens are refreshed within 60 seconds of expiry when used or health-checked. Health checks re-read identity/permissions at most five minutes apart during ordinary verification; the Health API forces a check. No worker or background task is required. Expiry is reflected in API reads immediately. Missing/expired refresh tokens require reconnect. Invalid grants and revoked permissions fail closed. A database lease coordinates refresh across instances; credentials and refresh-token rotation are committed with a revision check. A crashed/ambiguous refresh requires reconnect after the lease expires instead of replaying a potentially rotated token. Temporary health failures become `error` and can recover after a successful health check.

Disconnect increments the authorization generation before making network calls. Responses from concurrent refresh, OAuth or verification cannot restore the disabled state or turn an in-flight observation into successful evidence. Failed revocations are retained encrypted, including cleanup of rejected OAuth grants, and can be retried. Legacy GitHub PATs and grants the provider can no longer revoke programmatically must be revoked in provider settings before the explicit local-erasure action. This confirmation is an administrative audit event, never verification evidence. Google and GitHub grant revocation can affect other tokens for the same app/account; use dedicated partner accounts when separate grants must remain independent.

Registration binds each transition baseline to the actual connection/account (or anonymous public GitHub mode) in a separate tenant-scoped table. Account switches require disconnect first, and verification of an old baseline under a different account returns UNKNOWN. Baselines predating connection bindings must be registered again for managed transition verification. Existing receipts and their signed bytes/schema are unchanged; disconnect does not invalidate historical evidence.

Callback query parameters are stripped from the ASGI access-log scope before application handling. OAuth token exchange/revocation secrets are sent in POST bodies or authentication headers, never URL parameters. Logs and audit events contain only fixed error/action identifiers and projected metadata. At the ingress/CDN/APM layer, omit query strings for the two callback paths and disable request/response body capture for connection/OAuth endpoints: infrastructure sees a request before application middleware can scrub it. Do not record Authorization, Cookie or X-DoneProof-Key headers.

## Migration and legacy configuration

PostgreSQL schema migration 2 adds `connections`, `connection_oauth_states`, `connection_baseline_bindings` and `connection_revocations` in the existing advisory-lock transaction. It does not alter or rewrite existing contracts, receipts, baselines, idempotency keys, evidence or audits. SQLite receives the same additive tables for development. Repeated and concurrent startup is tested.

Existing `DONEPROOF_GMAIL_TOKENS_JSON` entries import once into encrypted connections for their explicit tenants. Global `GMAIL_ACCESS_TOKEN`/`GITHUB_TOKEN` import only when a single configured tenant can be determined, or `DONEPROOF_LEGACY_CONNECTION_TENANT` explicitly assigns ownership. Multi-tenant global fallback is rejected. Imports require encryption keys and a successful provider health check before reporting availability. A disabled connection is never resurrected by restarting with old environment variables. After validating imported connections, remove these deprecated token variables; managed OAuth then replaces access-only legacy grants.

For key rotation, deploy a ring containing both old and new keys with the new key active. Rotate each connection through the admin API (or refresh/reconnect it). Resolve pending revocations, allow outstanding OAuth states to expire, and verify all remaining envelopes/backups are covered before removing old keys. Never delete a key needed by retained credentials. Additive tables may remain during an application rollback; older binaries do not understand managed credentials, so plan rollback access separately.

## Validation

CI runs Ruff, the full suite on Python 3.11/3.12/3.13, Docker build, and the complete suite against PostgreSQL 16. Connection lifecycle tests run against both SQLite and isolated PostgreSQL schemas. Coverage includes additive migration/data preservation, concurrent initialization, tenant/admin isolation, authenticated encryption, key rotation, OAuth replay/browser/expiry/generation, refresh rotation/races, disconnect/revoke retries, rejected-grant cleanup, strict endpoints/scopes, baseline account binding and receipt integrity. Production smoke now checks the protected connections route, Settings page headers and console navigation.

Run locally:

```sh
python -m pip install -e '.[dev]'
ruff check doneproof tests scripts
pytest -q
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/doneproof_test pytest -q
docker build -t doneproof:phase1 .
```

Use a dedicated test database whose user can create/drop isolated test schemas. Provider tests use deterministic HTTP stubs and do not require live credentials. Complete a real partner authorization using the configured provider apps before production rollout; provider registration/consent approval is an external deployment prerequisite.

Provider references:
- [Google web server OAuth and revocation](https://developers.google.com/identity/protocols/oauth2/web-server)
- [GitHub App user access tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)
- [GitHub OAuth grant revocation](https://docs.github.com/en/rest/apps/oauth-applications)
