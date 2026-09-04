# DoneProof

**Outcome assurance infrastructure for AI agents.**

DoneProof independently verifies that an AI agent produced the external outcome a user requested, then emits a signed evidence receipt.

An executor saying **“done”** is never treated as proof.

## Why DoneProof

Agent systems are increasingly able to send email, update records, create pull requests, submit forms, trigger refunds, and operate internal business software. Tool-call success is not the same as outcome success.

DoneProof separates execution from assurance:

```text
human intent
    ↓
completion contract
    ↓
agent / automation performs work
    ↓
independent provider observation
    ↓
deterministic postconditions
    ↓
VERIFIED / PARTIAL / FAILED / UNKNOWN
    ↓
Ed25519-signed evidence receipt
```

## Pilot release

DoneProof `0.8.0` is designed for controlled industry pilots and integration experiments.

Supported evidence paths:

- **GitHub** — issues and pull requests, including time-bounded discovery when the final resource number is not known in advance.
- **Gmail** — distinguishes `SENT` from `DRAFT`, and verifies recipients, subject, thread and attachment metadata.
- **Trusted webhooks** — signed evidence events from ERPs, CRMs, payment systems, support platforms, internal APIs, or proprietary workflows.

The optional model compiler converts natural-language intent into a completion contract. The verification engine does not depend on a model and can be used immediately with explicit contracts.

## Core invariant

> A registered run cannot become `VERIFIED` unless every required postcondition independently passes against evidence observed after DoneProof registered the run.

DoneProof returns `UNKNOWN` when authoritative state cannot be established safely. It does not guess its way to success.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
cp .env.example .env
uvicorn doneproof.app:app --host 0.0.0.0 --port 8000
```

Open:

- Product surface: `http://localhost:8000/`
- Assurance console: `http://localhost:8000/console`
- API reference: `http://localhost:8000/docs`
- Readiness: `http://localhost:8000/ready`

Or run with Docker:

```bash
docker compose up --build
```

## Recommended high-assurance flow

### 1. Register the intended outcome before execution

```bash
curl -X POST http://localhost:8000/v1/runs \
  -H 'Content-Type: application/json' \
  -H 'X-DoneProof-Key: dp_live_example' \
  --data-binary @examples/github_registered_run.json
```

DoneProof stamps the trusted `task_started_at` boundary on the server. For mutable existing resources, set `require_change: true` on a postcondition; DoneProof captures a minimal pre-execution baseline and requires an unsatisfied → satisfied transition before crediting the agent.

### 2. Let the agent perform the action

DoneProof does not need to be the executor. The action can come from any model, agent framework, browser agent, RPA system, or internal automation.

### 3. Verify the registered run

```bash
curl -X POST http://localhost:8000/v1/runs/cc_xxx/verify \
  -H 'X-DoneProof-Key: dp_live_example' \
  -H 'Idempotency-Key: agent-run-1842'
```

The response is a signed `VerificationReceipt` with condition-level evidence.

## Verdicts

| Verdict | Meaning |
|---|---|
| `VERIFIED` | Every required outcome independently passed. |
| `PARTIAL` | Required outcomes contain both pass and fail results. |
| `FAILED` | Required outcomes failed and none passed. |
| `UNKNOWN` | Required authoritative state could not be established safely. |

Optional postconditions never downgrade a fully satisfied required outcome.

## Signed receipts

Receipts are signed with Ed25519 and include:

- original task and contract ID
- assurance level (`registered`, `submitted`, or `synthetic`)
- condition-level PASS / FAIL / UNKNOWN
- minimal observed evidence
- provider source reference
- per-condition latency
- overall duration
- SHA-256 receipt hash
- Ed25519 public key ID and signature

Integrity can be checked with:

```text
GET /v1/receipts/{receipt_id}/integrity
```

A human-readable certificate is available at:

```text
GET /v1/receipts/{receipt_id}/certificate
```

## Security posture

DoneProof intentionally avoids a generic arbitrary-URL verifier. Provider adapters use constrained endpoints, and webhook ingestion requires HMAC authentication plus a replay window.

Production deployments should configure:

- workspace API keys
- a persistent Ed25519 signing seed from a secret manager/KMS bootstrap
- tenant-specific Gmail credentials where Gmail verification is required
- webhook source secrets isolated from execution agents
- TLS termination and network policy at the deployment edge
- durable database backup and retention policy

See [`docs/SECURITY.md`](docs/SECURITY.md).

## Customer integrations

See:

- [`docs/INTEGRATION_GUIDE.md`](docs/INTEGRATION_GUIDE.md)
- [`docs/PILOT_GUIDE.md`](docs/PILOT_GUIDE.md)
- [`docs/RECEIPTS.md`](docs/RECEIPTS.md)

Example contracts are under [`examples/`](examples/).

## Test

```bash
pytest -q
python -m compileall doneproof
python benchmarks/benchmark_core.py
```

The test suite covers success, partial completion, uncertainty, tenant isolation, signature tampering, idempotency, transient provider failures, GitHub resource ambiguity, Gmail draft-vs-sent behavior, webhook authentication/replay protection, temporal-boundary enforcement and upgrade-safe persistence.

## What DoneProof proves

DoneProof proves that configured postconditions match independently observed evidence under the configured trust model. It does **not** prove that an ambiguous human instruction was interpreted correctly, that an external provider is truthful, or that an authorized operator intended the action. Those belong to intent governance, authorization and provider trust respectively.

That boundary is deliberate: DoneProof is the **outcome assurance** layer between agent execution and business acceptance.
