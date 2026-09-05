# Assurance Session Protocol 1.0

An assurance session binds one workspace outcome to Compiler v2, one immutable
registered contract, its server-established boundary and baselines, provider
versions, durable jobs and an immutable receipt chain. It contains no executor
trace. Only the existing provider adapters observe evidence.

## Python

Install this stacked revision with `pip install -e .`. `DoneProofClient` and all
of its pilot methods remain supported unchanged; new integrations should use
`DoneProof` or `AsyncDoneProof`. Both share one transport/polling implementation.
The synchronous facade owns one background event loop; always close it or use
a context manager. Custom transports use `httpx.AsyncBaseTransport` in either
new client. Python 3.11–3.13 are tested.

```python
import os
from doneproof import DoneProof

def assure_invoice(run_agent, repair_agent):
    with DoneProof(api_key=os.environ['DONEPROOF_API_KEY']) as dp:
        session = dp.assurance.prepare(
            task='Send email to alice@company.com with subject "Invoice #1842" with attachment "invoice-1842.pdf"',
            idempotency_key='invoice-1842-send-v1',
        )
        if session.state != 'READY_FOR_EXECUTION':
            return session  # Inspect typed compiler.clarification_requirements.
        run_agent()  # Your existing runtime and credentials; DoneProof does not execute it.
        result = dp.assurance.verify(session.id, wait=True, timeout=120)
        if result.verdict in {'PARTIAL', 'FAILED', 'UNKNOWN'} and result.can_reverify:
            repair_agent(result.remediation)  # Guidance is not evidence.
            result = dp.assurance.reverify(
                session.id, previous_receipt_id=result.receipt.receipt_id,
                idempotency_key='invoice-1842-repair-1', wait=True,
            )
        if result.receipt:
            assert dp.verify_receipt(result.receipt, os.environ['DONEPROOF_PINNED_PUBLIC_KEY'])
        return result
```

The async client has identical methods with `await` and `async with`. Typed
models are exported from `doneproof.sdk_models`; errors, cancellation and log
records are in `doneproof.sdk_common`.

## TypeScript / Node

Build the package from `sdk/typescript` with `npm ci && npm run build`. It is an
ES module targeting Node 22+, with no frontend or agent-framework dependency.
The package is prepared for publication but is not published by this PR.
Use a server-side API key; browser embedding of this Node package is unsupported.

```ts
import { DoneProof, verifyReceipt } from '@doneproof/sdk';

const dp = new DoneProof({ apiKey: process.env.DONEPROOF_API_KEY! });
const session = await dp.assurance.prepare({
  task: 'Close issue #12 in acme/api', idempotencyKey: 'issue-12-close-v1'
});
if (session.state === 'READY_FOR_EXECUTION') {
  await agent.run(); // Existing customer-owned agent.
  const result = await dp.assurance.verify(session.id, { wait: true });
  if (result.receipt && result.signed_payload_b64) {
    if (!verifyReceipt(result.receipt, result.signed_payload_b64,
                       process.env.DONEPROOF_PINNED_PUBLIC_KEY!)) {
      throw new Error('Untrusted receipt');
    }
  }
}
```

API field names remain snake_case in both SDKs. Method options use each language's
conventions. TypeScript types and local runtime JSON Schemas are generated from
the Python models; CI rejects generated-file drift. Unknown protocol/provider
SDK versions, invalid clarifications and receipt schema downgrades fail closed.

## HTTP surface and idempotency

| Route | Input | Result |
| --- | --- | --- |
| `POST /v1/assurance/sessions` | task, declared planning context; Idempotency-Key required | typed session |
| `GET /v1/assurance/sessions/{id}` | workspace key | current session, jobs and lineage |
| `POST /v1/assurance/sessions/{id}/verify` | deadline_seconds, callback_id; key required | session with durable job |
| `POST /v1/assurance/sessions/{id}/reverify` | previous_receipt_id plus scheduling options; key required | linked recovery job |
| `GET /v1/assurance/sessions/{id}/receipt` | workspace key | current completed receipt; 409 if unavailable/in flight |
| `POST /v2/connections/{id}/disconnect` | expected_revision; administrator key and idempotency key | current connection status |

Preparation keys identify customer business operations, not task text. Reusing
the same text may represent a genuinely different invoice or action, so the SDK
requires an explicit persisted key. Save the returned session ID before executing.
Concurrent preparation returns the same ID, possibly in PREPARING state; fetch
that session until ready. It never captures a second boundary on retry. An
interrupted/expired preparation becomes PREPARATION_FAILED and requires a new
operation key **before** any external execution. A clarification is persisted as
NEEDS_CLARIFICATION with no registered contract; revise the task/connection and
prepare with a new key.

Initial verify uses a deterministic key and converges on one initial job even
across different retry keys. Conflicting scheduling options return 409.
Re-verification requires both the exact previous receipt and a caller-persisted
attempt key. An old attempt cannot silently advance a newer chain head. Concurrent
re-verification is rejected by the existing recovery chain lock. Responses can
reflect a later current session state when an older request is replayed.

`wait_for_verification` / `waitForVerification` resumes from a job or session ID.
Polling uses equal jitter, a 0.25-second initial interval, a 5-second ceiling and
one absolute deadline covering submission, retries, polling and final retrieval.
Transient 429/5xx/network errors retry at most four transport attempts, respecting
Retry-After; semantic verdicts are never retried. Python `Cancellation` and Node
AbortSignal stop local waiting. `cancel_job` / `cancelJob` requests durable server
cancellation; stopping a client does not implicitly cancel server work. An
expired/internal-error job produces session state UNKNOWN with no invented
receipt or verdict. Prior receipts remain in lineage.

## Connections and capabilities

`list_connections`, `connection_status`, `begin_connection`, `disconnect` (or
camelCase equivalents) require a workspace **connection administrator** key.
Do not give that key to an untrusted executor. Onboarding returns the trusted
DoneProof `/connections` page. The administrator selects the provider there;
OAuth starts in that browser and its callback validates the existing HttpOnly
cookie, state and PKCE binding. The SDK never generates a provider OAuth URL in
its own cookie jar or handles provider client secrets.

Disconnect takes the revision from a prior connection-status response. A stale
revision fails closed; replaying a completed operation returns current status
without disabling a subsequently reconnected account. Revocation may remain
pending after a provider outage; inspect live status and use the existing
connection management flow to retry/confirm revocation.

`providers.available()` returns live tenant capabilities. `providers.declarations()`
caches immutable declaration snapshots for at most 60 seconds and returns copies.
`providers.for_task()` / `forTask()` delegates planning to Compiler v2; it has no
local authority to invent identifiers or select fallback providers. Compilation
planning calls are not automatically retried because they may incur model cost.

## Receipt assurance and callbacks

Session evidence summaries explicitly report `provider`, `evidence_class`,
`assurance_level` and `provenance`. `provider_declared` does not claim that every
plugin is a trusted API. Browser evidence is always `browser_ui` /
`lower_than_authoritative_api`. No SDK or session route performs API-to-browser
fallback. Executor screenshots, tool output, reasoning, success flags and page
state are not accepted by session preparation or verification routes.

The receipt remains byte-for-byte compatible with schemas 1.0/1.1/1.2.
`signed_payload_b64` is the exact existing canonical signed payload, not a new
signature format. TypeScript verifies these bytes against an independently
pinned Ed25519 key and checks that the parsed payload matches the displayed
receipt. This avoids floating-point JSON serialization differences across
languages. Never trust a key solely because the same API response supplied it.

Callbacks name an operator-configured tenant callback ID; clients cannot provide
arbitrary destinations. Verify raw bytes before parsing/dispatching:

```python
from doneproof.sdk_callbacks import verify_callback
from doneproof.sdk_common import DuplicateEvent

def receive(raw_body, headers, callback_secret, durable_receiver):
    try:
        event = verify_callback(raw_body, headers, secret=callback_secret,
                                replay_store=durable_receiver)
    except DuplicateEvent:
        return 200  # Already durably queued; acknowledge the provider retry.
    # durable_receiver.claim must atomically persist the event in your receiver's
    # inbox. Dispatch from that inbox, so a crash after claim cannot lose work.
    return event
```

TypeScript `await verifyCallback(bytes, headers, {secret, replayStore})` has the
same contract. Signature verification and deduplication are mandatory. The body
event ID must match the header/job identity. Default skew is 300 seconds, maximum
600; replay entries survive the 24-hour delivery horizon. `MemoryReplayStore` is
for single-process development only; production requires a shared durable inbox
whose atomic `claim(event_id, expires_at)` coordinates all receiver instances.
Callback delivery acknowledges verification completion, not business success.
Fetch the tenant-owned job/session and verify its receipt separately.

Structured log hooks contain only method, status, local request ID, attempt and
duration. SDK errors omit response bodies, request objects and provider error
text. Application code remains responsible for not logging secrets or customer
objects itself.

## Migration and deployment

Additive migration 7 adds preparation reservations, session-job associations and
revision-bound connection operations. Migrations 1–6 and existing receipt bytes
remain intact. Preparation publishes contract/baselines/provider bindings/audit
registration/session readiness in one transaction. Network I/O occurs outside
transactions. Sessions do not add a new job format: Phase 6 workers can process
these existing registered jobs safely. Older workers encountering unsupported
provider declarations or recovery payloads retain the existing fail-closed
checks. Older API instances return unavailable routes; SDK validation never
treats that as a successful session.

The API still needs persistent workers; Vercel does not run a background worker
inside an HTTP request. Browser work retains its separate Chromium/sandbox image.
Roll out compatible APIs before exposing the new SDK flow. Production smoke is
prepared and remains gated to an intentional main release. This stacked change
is not merged and is not deployed to production.

## Runnable fixtures

`python examples/assured_agent.py` and, from `sdk/typescript`, `npm run demo`
exercise prepare → false claim → independent failure → external fixture repair
→ linked VERIFIED receipt → pinned verification. Set `PYTHON` if the Node demo
needs a specific Python interpreter. These use explicit offline fixture state,
not design-partner accounts or real external-provider evidence.
