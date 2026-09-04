# Changelog

## 0.9.4

- Centralized runtime/client/provider version reporting to prevent stale integration identifiers.
- Added trusted-key receipt verification so customers can distinguish cryptographic integrity from issuer authenticity.
- Clarified evidence-bundle and signing-key trust semantics across the API and documentation.
- Documented fail-closed serverless startup diagnostics and the production readiness acceptance check.
- Refreshed pilot-readiness documentation for durable PostgreSQL deployments.

## 0.9.3

- Made the serverless entrypoint fail closed with a sanitized 503 diagnostic app instead of crashing during import when production configuration or storage initialization is invalid.
- Added stable startup error classes for API-key, signing-key, database URL and storage failures so `/ready` is actionable without exposing secrets.
- Kept `create_app()` strict for tests and production safety while making Vercel runtime failures observable.

## 0.9.2

- Added first-class PostgreSQL persistence selected through `DATABASE_URL` / `POSTGRES_URL`.
- Added idempotent PostgreSQL schema bootstrap guarded by an advisory transaction lock for concurrent serverless cold starts.
- Made production fail closed unless durable PostgreSQL storage is configured.
- Added real PostgreSQL integration coverage to GitHub CI.
- Exposed storage backend and durability in `/ready`.
- Kept SQLite only as a zero-setup local-development and Vercel smoke-test fallback.
- Preserved the explicit Vercel FastAPI entrypoint and Python 3.12 deployment runtime.

## 0.9.1

- Fixed Vercel deployment detection with an explicit FastAPI entrypoint and Python 3.12 pin.
- Added a Vercel-safe `/tmp/doneproof.db` fallback for deployment smoke tests when no database path is configured.
- Bound every signed receipt to the exact canonical completion contract with `contract_hash`.
- Made historical receipt integrity independent of the server's current signing key so key rotation does not invalidate old receipts.
- Made completion-contract IDs immutable within a workspace.
- Removed internal mock/synthetic vocabulary from the customer OpenAPI schema when demo mode is disabled.
- Changed the pilot console to session-only API-key storage.
- Reworked customer documentation around independent outcome assurance, trust boundaries and measurable pilot value.
- Added a single architecture guide, product overview and buyer FAQ.
- Expanded regression and trust-model coverage to 53 tests.

## 0.9.0

- Added bounded batch verification for pilot datasets and replay experiments.
- Added portable evidence bundles containing receipt, integrity result and public signing key.
- Added tenant-scoped audit events for registered runs, verifications and accepted webhook evidence.
- Added configurable per-workspace pilot request limiting and batch-size controls.
- Added response latency headers and a stricter browser content-security policy.
- Added a minimal Python client for faster customer integration.

## 0.8.0

- Added server-registered high-assurance run lifecycle.
- Added Gmail verification and signed enterprise webhook evidence.
- Added tenant isolation, Ed25519 receipts, idempotency, retries, timeouts and assurance console.
