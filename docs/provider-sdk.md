# Provider SDK v1

DoneProof discovers trusted installed adapters through one immutable `ProviderRegistry`. The compiler, capabilities API, engine, durable workers, connection settings and generated reference consume that registry. No HTTP request can install code, select a Python import, or register a verification URL.

## Implementing a provider

Implement `ProviderAdapter.observe(selector, ObservationContext)` and return `ProviderObservation`. Context includes the tenant, contract, condition and server-owned time boundary. Observe independently from the authoritative system; do not execute the requested business action. Return `indeterminate=True` when authorization, search completeness, identity, freshness or response validity is uncertain. `state=None` is authoritative absence only when the provider can establish absence. Never turn a permission denial into absence.

Export a zero-argument function returning `ProviderDefinition` from your adapter package. Its `ProviderManifest` declares ID, semantic version, resource types, self-contained JSON selector/evidence schemas, predicate operators, discovery identity and scope, temporal boundary, baseline/transition support, authentication, rate limits, sensitivity and optional additional redaction paths. Declarations reject duplicate/reserved IDs, unknown operators, invalid limits, external schema references and unsafe OAuth origins. Metadata is copied before exposure and cannot mutate a running registry.

Declare the package entry point:

```toml
[project.entry-points."doneproof.providers"]
inventory = "your_inventory_adapter:provider_definition"
```

Install that package on both API and worker images. The entry-point name must equal the declared provider ID. No DoneProof provider list needs editing. Applications embedding DoneProof can instead pass `ProviderRegistry([...])` to `create_app(provider_registry=...)`. Registries are application-scoped; test overrides can replace existing adapters but cannot introduce an undeclared provider.

`tests/sdk_provider.py` is a complete fourth-provider fixture. `tests/test_provider_registry.py` exercises it through OAuth, compilation, synchronous verification, durable jobs, retry/deadline policy, refresh, disconnect, publication fencing, restart, version changes, tenant isolation and redaction. It is a simulator for SDK tests and is never installed as a production provider.

## Provider-owned hooks

- `adapter_factory(AdapterRuntime)` returns a fresh `ProviderAdapter`. Managed adapters receive the current tenant's decrypted credential bundle only inside this call. Use the supplied response hooks with HTTP clients so the connection manager can detect revocation and durable transient failures. Fix provider endpoints in adapter code; reject redirects and never accept an arbitrary verification URL.
- `CompilerHooks` implements full-clause parsing, condition validation, intent validation and legacy-selector validation. `SchemaCompiler` supplies conservative defaults: an exact explicit grammar, no broad discovery, literal/typed-context grounding, declared evidence fields and typed predicates. Subclass it for exact provider grammar and additional deterministic rules. Extend both selector validation and intent analysis when supporting discovery or provider-specific verbs. Selector preflight uses the declared identity schema and scope fields, and rechecks discovered constraints before pinning an identity.
- A managed OAuth provider supplies `ConnectionBackend(settings, transport)` with configuration detection, PKCE authorization URL, exchange, refresh, stable account identity/health validation and revoke. Return normalized credential dictionaries containing `access_token`, optional `refresh_token`, absolute epoch-second expiries, `scopes` and `kind`. Enforce read-only upstream permissions in `identity`. Raise `ProviderFailure` with a standard SDK diagnostic, never upstream response text. The shared service owns encryption, browser/state binding, single-use callback state, refresh leases, account continuity, connection revisions and revoke recovery.
- Unmanaged providers supply tenant-bound `capability(tenant, settings)`. Event providers also supply `event_selector_allowed(tenant, selector, settings)`; compilation cannot defer evidence from an unowned source. The webhook adapter retains its existing signed-ingestion semantics.
- Optional configuration validation, legacy credential import and installation-link hooks remain provider-owned. The registry lists only adapters with managed OAuth in Connection Settings. Browser authorization destinations must match their declared HTTPS origin.

The SDK does not accept provider-defined predicate execution or receipt signing. All adapters use DoneProof's deterministic predicate engine, UNKNOWN rules, registered baselines, verdict calculation, remediation rejection and signing code. Capability and model confidence never count as evidence. Evidence schemas describe the normalized observation contract; adapters must validate and normalize upstream data, with conformance tests covering malformed and incomplete responses. Existing built-in normalization and observation behavior are retained.

Omit `compiler` from `ProviderDefinition` to use the schema-only fast path. For example, with an `inventory` provider declaring resource type `item`, identity `item_id` and string field `status`, the exact task `Verify inventory item "item-0" has status = "ready"` compiles without model calls or provider-specific compiler code. JSON literals supply identity and expected value. More natural provider vocabulary is optional and belongs in provider-owned compiler hooks.

## Retry and evidence rules

Use `TransientObservationError` only for transient infrastructure failures; the durable worker persists bounded attempts and delays. The shared HTTP helper classifies 429, retryable 5xx and network failures, honors Retry-After and uses exponential backoff with jitter. Built-in GitHub/Gmail quota semantics remain unchanged. Provider-specific transient cases can be classified within the adapter. Semantic failures, missing outcomes, ambiguous resources and ordinary permission denials are not transient infrastructure failures.

`rate_limit.concurrency` controls shared PostgreSQL provider slots as well as local verification concurrency; `preflight_concurrency` bounds planning requests. No network call occurs while holding a database publication transaction. Additional `sensitive_paths` redact whole named fields before durable observation storage and force predicates depending on those fields to UNKNOWN. Redact an entire array if its elements contain sensitive fields. Global credential-key filtering cannot be disabled. Upstream exceptions become fixed diagnostics; never put tokens into adapter notes, resource URLs, account labels or normalized evidence.

## Versioning and rollout

Bump the adapter semantic version whenever observation, normalization, authentication, discovery or compiler semantics change. New jobs store fingerprints of the declared versions, schemas and policies. A restarted worker with a missing or different declaration terminates the job as `INTERNAL_ERROR` with `provider_definition_changed`; it cannot sign a receipt using a different declaration. Metadata fingerprints do not hash Python implementation code, so version discipline is required.

New registered runs bind their provider declarations before baseline capture. Verification after a declaration change returns UNKNOWN and requires a new run. Existing pre-SDK receipts retain their exact canonical payloads and signatures. Pre-SDK jobs use the shipped built-in declarations for compatibility; pre-SDK runs retain their original behavior because no provider-version binding was recorded.

Migration 5 runs under the existing startup migration lock. PostgreSQL removes the historical Gmail/GitHub connection check and adds job manifest storage and tenant-scoped registration bindings. SQLite transactionally replaces the constrained parent table while preserving encrypted OAuth state and pending revocations with foreign keys enabled. Neither migration rewrites signed receipts, baselines, credential ciphertext or IDs. Test coverage includes concurrent starts and rollback after an injected failure.

Deploy matching adapter packages to APIs and workers. Drain workers before reducing concurrency or changing adapter versions; older binaries cannot enforce SDK v1 version fencing. Schema migration is forward-compatible with old stored data, but rolling back to old software after admitting a new provider is unsupported. No migration or deployment to production is performed by the test suite.

## Documentation and validation

`GET /v1/providers` returns declaration metadata without credentials and requires a workspace verification key. `GET /v1/connections/provider-metadata` returns onboarding display names and fixed OAuth origins for an authorized connection administrator. Existing capabilities and connection-list response shapes are preserved. OpenAPI provider enums and the model's structured-output schema use the installed catalog.

Generate the reference with `python scripts/provider_docs.py`; add `--installed` for installed plugins. CI checks that `docs/providers.md` matches the shipped declarations. Run Ruff, the complete pytest suite with `TEST_DATABASE_URL` pointing to an isolated PostgreSQL test database, compiler corpus evaluation and Docker build before releasing an adapter.

The packaging mechanism follows the [PyPA entry-point specification](https://packaging.python.org/en/latest/specifications/entry-points/). Schema validation uses [jsonschema Draft 2020-12 validators](https://python-jsonschema.readthedocs.io/en/stable/validate/); schemas are checked once before repeated validation.

Optional `ProviderDefinition.admit_condition` rejects provider-specific unsafe selectors/predicates at HTTP admission. `ProviderAdapter.validate_postcondition` repeats that check during evaluation; `default_provenance` supplies source classification on inconclusive observations. `ProviderObservation.provenance` survives durable checkpoints and signing in receipt 1.2; missing provenance remains omitted in historical API receipts. Publication calls `observation_is_current` for unmanaged adapters as well as the existing transactional managed-connection fence. Compiler hooks may supply `compilation_warnings()`; the browser adapter uses this for its lower-assurance warning. These additive hooks retain SDK v1 defaults for existing providers.
