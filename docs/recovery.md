# Failure explanations and independent re-verification

DoneProof explains failed conditions and schedules fresh observations. The executor
performs any business repair in the external system. Recovery has no send, modify,
merge, refund, or other business-action interface.

Newly issued receipts use **receipt schema 1.1**. Completion contracts and existing
API paths retain their versions and limits. Receipt 1.1 signs these additional fields:

```json
{
  "remediation": [{
    "kind": "doneproof.remediation",
    "condition": "p1",
    "status": "FAIL",
    "expected": "sent",
    "observed": "draft",
    "retryable": true,
    "code": "message_is_draft",
    "action_hint": "The message exists but is not in SENT state.",
    "reverify_after": "external_action"
  }],
  "previous_receipt_id": null,
  "previous_receipt_hash": null,
  "recovery": {
    "chain_id": "vr_original",
    "attempt": 0,
    "oscillating_conditions": [],
    "repeated_failures": []
  }
}
```

Guidance is deterministic, derived only from condition results and fixed rules.
It never changes a predicate, verdict, or evidence record. UNKNOWN remains UNKNOWN.
`retryable` describes whether the condition can be checked again under the original
contract; the chain's `can_reverify` also accounts for its budget and active job.
A missing, unknown, or already-satisfied transition baseline cannot be repaired
after execution. Such guidance calls for a new run registered before a new action.

Existing schema 1.0 receipts retain their exact canonical signing payload. They
are neither rewritten nor re-signed. Update clients that validate closed receipt
schemas to accept 1.1 before deploying; the included Python client accepts both.
Pin signer keys independently, including authorized key rotations. A chain hash
proves linkage and integrity, not issuer trust by itself.

## Executor integration

All recovery APIs require the existing workspace API key, scoped to its tenant.

| Endpoint | Behavior |
| --- | --- |
| `GET /v1/receipts/{id}/remediation` | Guidance for this receipt; derives a sidecar for legacy receipts. |
| `POST /v1/receipts/{id}/reverify` | Queue fresh verification of the latest receipt in its chain. Requires `Idempotency-Key`. |
| `GET /v1/receipts/{id}/history` | Ordered receipt and condition transitions, hashes, attempts, active job and remaining eligibility. |
| `POST /v1/receipts/{id}/recovery-policy` | `{"automatic": true}` or `false` opts the chain into authenticated evidence triggers. |

Re-verification accepts only `deadline_seconds` (1–3600, default 300) and an optional
preconfigured workspace `callback_id`. It returns the existing durable job shape
with HTTP 202, or 200 for an identical idempotent retry. Use `/v1/jobs/{id}`,
`/wait`, `/conditions`, and `/cancel` as before. Completion callbacks identify the
new receipt, which includes the signed remediation and parent link. There are no
request-supplied callback URLs, observations, repair claims or replacement contracts.

The original contract, task-start boundary, assurance level and signed baseline
are frozen on enrollment. Every retry reads **all** conditions independently,
including conditions that previously passed. Managed connector account bindings,
redaction, provider retry policies, deadlines and publication fences still apply.
Historical receipts can enroll only when the exact original contract is available
and matches its signed contract hash; otherwise recovery fails closed. Large durable
job contracts use their original persisted job snapshot.

One active job is allowed per chain. Concurrent identical requests return the same
job. A different request during execution or against an old head returns 409.
The attempt reservation and job creation commit together. Publication commits the
new receipt, parent hash, chain head, job terminal state and callback outbox together.
Cancellation, expiry and internal errors consume their reserved attempt, issue no
receipt, and leave the last signed head unchanged. Restart does not reset budgets.

## Limits and authoritative events

Set `DONEPROOF_MAX_REVERIFICATION_ATTEMPTS` to an integer **0–20**, default **5**.
Zero disables new recovery attempts. Each chain freezes its limit at enrollment;
the effective limit is the smaller of that value and current deployment settings.
Increasing deployment settings does not silently expand an existing chain's budget.
The limit is per recovery chain; existing synchronous verification APIs remain
available for independent verifications.

Run `python -m doneproof.worker` on persistent compute alongside the API. Its
recovery loop resumes the PostgreSQL event queue after a restart. Automatic recovery
is opt-in and currently uses DoneProof's existing authenticated webhook ingestion.
It requires exact, workspace-configured `source`, `event_type` and `object_id`
selectors. Gmail and GitHub repairs can use explicit re-verification; this release
does not install provider push subscriptions.

Only a successfully authenticated webhook insert can enqueue a trigger. Insertion
and enqueueing are atomic. Replays, foreign tenants, different resources and
timestamps no newer than the relevant provider observation cannot trigger a retry.
Event and observation timestamps must establish that ordering; coarse timestamps
within the same second can require a later event or explicit re-verification.
Events arriving during a job remain pending and are reconsidered against its latest
receipt. Events observed by that job need no further attempt. No polling or generic
arbitrary-URL verification is introduced.

Four alternating condition outcomes (including alternating observed failure values)
flag oscillation. Three identical consecutive non-PASS outcomes flag repeated
failure. Oscillation flags remain in subsequent linked receipts. Automatic attempts
pause for either pattern; an executor may still request a bounded manual check
after an external repair. History exposes both warnings and canceled/expired jobs.

Remediation envelopes, copied receipts and reserved guidance predicate fields cannot
be used as outcome evidence, even when echoed in a trusted webhook payload. Guidance
is never inserted into the evidence store by DoneProof. The webhook source remains
responsible for truthfully reporting its authoritative business state: possession of
its signing secret is that source's trust boundary, not an executor capability.

## Migration and operations

Migration 4 is additive on PostgreSQL and SQLite. Existing contracts, baselines,
receipts, credentials, idempotency records and queued jobs are preserved. New
composite foreign keys enforce tenant ownership. Database triggers prevent
application UPDATE/DELETE operations on receipts and immutable recovery facts.
Normal database-administrator backup and retention authority is unchanged.
Bootstrap uses the existing advisory lock and tolerates concurrent startup.

Deploy updated API and worker code together before enabling automatic recovery.
Drain old workers before routing recovery jobs to the shared queue, because older
workers do not publish recovery links. Database publication guards reject an old worker's unlinked receipt and release its failed attempt without changing the signed head. Rollback must likewise drain recovery jobs
before starting older workers. Keep the additive tables when rolling back.

The assurance console exposes a History button on each receipt, the immutable
transition history, deterministic guidance, bounded re-verification, cancellation
and the webhook policy. It renders receipt content as text and clears recovery
state when the workspace key changes.
