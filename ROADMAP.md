# DoneProof — build sequence

## Day 1 — Core invariant (done)

Invariant: **No task is VERIFIED unless every required postcondition is independently observed and passes.**

Delivered:
- completion-contract domain model
- deterministic predicate engine
- `VERIFIED / PARTIAL / FAILED / UNKNOWN`
- mock adapter
- GitHub issue/PR observer
- signed verification receipts
- SQLite persistence
- Astra structured-output compiler
- FastAPI API + Swagger UI
- tests + Dockerfile

## Day 2 — GitHub discovery (done)

DoneProof no longer needs to trust an executor-supplied issue/PR number for a newly created resource.

Delivered:
- `task_started_at` boundary on completion contracts
- issue/PR discovery when `number=null`
- exact title / author / PR head-branch constraints
- client-side creation-time enforcement
- bounded pagination
- duplicate candidate detection → `UNKNOWN`, never guessed success
- GitHub privacy-preserving 404 → `UNKNOWN`, not false `FAILED`
- unique candidate re-fetch from canonical detail endpoint before predicates run
- adversarial tests for wrong resource, pre-existing resource and duplicate title

Acceptance behavior: if an executor creates the wrong issue but claims the requested issue was created, DoneProof independently searches post-task GitHub state and returns `FAILED`.

## Day 3 — Gmail verifier

- Google OAuth flow
- read-only Gmail scope for verifier
- Sent mailbox observation
- recipient / subject / thread / attachment metadata predicates
- task-start timestamp bounding
- never infer success from draft existence

Acceptance test: message is left in Drafts; executor claims sent; verdict must be FAILED.

## Day 4 — Calendar verifier

- Google Calendar OAuth
- event lookup bounded by creation time
- attendee, timezone, time, recurrence and conference-link predicates
- distinguish event creation from invitation delivery state where API allows

Acceptance test: event exists but attendee is missing; verdict must be PARTIAL.

## Day 5 — Repair protocol

Define a provider-neutral machine response for failed postconditions:

```json
{
  "verdict": "PARTIAL",
  "repairable": true,
  "failed": [
    {"id":"p2", "reason":"alice not present in assignees"}
  ]
}
```

- idempotency key per task
- retry budget
- executor callback/webhook
- verify → repair → reverify state machine
- never let verifier perform unapproved side effects itself

## Day 6 — Browser fallback

Only for systems lacking a usable API.

- bounded allowed-domain browser session
- screenshot + DOM evidence bundle
- Astra semantic verifier for non-deterministic UI text
- confidence cannot promote an UNKNOWN deterministic outcome to VERIFIED
- screenshot hash in receipt

## Day 7 — Sellable integration

- TypeScript SDK
- Python SDK
- middleware wrapper around agent tool calls
- hosted API deployment
- public receipt viewer
- landing page + 100-task reliability benchmark

North-star demo: run an executor through intentionally flaky workflows and show the gap between **claimed completion rate** and **verified completion rate**.
