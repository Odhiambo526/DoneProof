# DoneProof

**Independent outcome assurance for AI agents.**

Managed Gmail/GitHub onboarding is available through **Assurance console → Connection Settings**. See [managed connection setup, APIs and migration](docs/managed-connections.md) for operator configuration and security boundaries.

Durable verification is available through `/v1/jobs`, with a separate PostgreSQL-backed worker. The synchronous API remains available. See [job APIs, retry/recovery semantics and deployment](docs/durable-verification.md).

AI agents can say a task is complete. DoneProof checks the external system and determines whether the requested outcome is actually true.

It works independently of the executor: OpenAI, Anthropic, Google, browser agents, RPA, internal agents, or conventional automation can all be verified through the same assurance layer.

> **Execution tells you what the agent attempted. DoneProof tells you what became true.**

## Hosted pilot

The current production pilot is available at **https://www.getdoneproof.com**.

- 90-second false-success demo: https://www.getdoneproof.com/demo
- Assurance console: https://www.getdoneproof.com/console
- API reference: https://www.getdoneproof.com/docs
- Readiness: https://www.getdoneproof.com/ready

Production runs on Vercel with durable PostgreSQL persistence on Neon. Workspace API routes require a DoneProof API key.

## The problem

An agent can report success when an email is still a draft, a GitHub issue has the wrong assignee, a refund was requested but never completed, or a CRM update landed on the wrong record.

Tracing and agent observability help explain **how the agent behaved**. DoneProof answers a different question:

> **Did the business outcome actually happen?**

## How it works

```text
1. Define the outcome
        ↓
2. DoneProof registers the verification contract
        ↓
3. Any agent or automation performs the work
        ↓
4. DoneProof independently reads authoritative state
        ↓
5. Deterministic postconditions are evaluated
        ↓
6. VERIFIED / PARTIAL / FAILED / UNKNOWN
        ↓
7. Ed25519-signed evidence receipt
```

DoneProof never accepts the executor's own success message as evidence.

## What can be verified today

- **GitHub** — issues and pull requests, including safe discovery of newly created resources.
- **Gmail** — sent vs draft, recipients, subject, thread and attachment metadata.
- **Trusted webhooks** — signed outcome evidence from CRMs, ERPs, payment systems, support platforms and internal services.
- **Batch evaluation** — compare reported agent success against independently verified outcomes across pilot datasets.

The natural-language contract compiler is optional. Explicit completion contracts work without any model dependency.

## Verdicts

| Verdict | Meaning |
|---|---|
| `VERIFIED` | Every required outcome is supported by authoritative evidence. |
| `PARTIAL` | Some required outcomes passed and others failed. |
| `FAILED` | Required outcomes failed and none passed. |
| `UNKNOWN` | DoneProof could not establish authoritative state safely. |

`UNKNOWN` is intentional. An assurance system should refuse to certify what it cannot prove.

## Recommended assurance flow

### 1. Register before execution

```bash
curl -X POST http://localhost:8000/v1/runs \
  -H 'Content-Type: application/json' \
  -H 'X-DoneProof-Key: dp_live_example' \
  --data-binary @examples/github_registered_run.json
```

DoneProof establishes the trusted start time. For updates to existing resources, `require_change: true` records a pre-execution baseline and requires an unsatisfied → satisfied transition.

### 2. Run the agent

Your existing executor performs the task. DoneProof does not need to control execution.

### 3. Verify the outcome

```bash
curl -X POST http://localhost:8000/v1/runs/cc_xxx/verify \
  -H 'X-DoneProof-Key: dp_live_example' \
  -H 'Idempotency-Key: agent-run-1842'
```

The response is a signed verification receipt containing the exact contract hash, condition-level evidence, verdict, timing and signature.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
cp .env.example .env
uvicorn doneproof.app:app --host 0.0.0.0 --port 8000 --env-file .env
```

Open:

- Product page: `http://localhost:8000/`
- Assurance console: `http://localhost:8000/console`
- API reference: `http://localhost:8000/docs`
- Readiness: `http://localhost:8000/ready`

Or:

```bash
docker compose up --build
```

## Documentation

- [Product overview](docs/PRODUCT_OVERVIEW.md)
- [Design partner pilot](docs/DESIGN_PARTNER_PILOT.md)
- [Architecture and trust model](docs/ARCHITECTURE.md)
- [Integration guide](docs/INTEGRATION_GUIDE.md)
- [Industry pilot guide](docs/PILOT_GUIDE.md)
- [Pilot report template](docs/PILOT_REPORT_TEMPLATE.md)
- [Receipt format and verification](docs/RECEIPTS.md)
- [Security model](docs/SECURITY.md)
- [Pilot deployment](docs/DEPLOYMENT.md)
- [Python client](docs/PYTHON_CLIENT.md)
- [FAQ](docs/FAQ.md)

## Security principles

- The executor's claim is never evidence.
- Registered runs use server-established timing boundaries.
- Mutable outcomes can require pre/post transition proof.
- Provider adapters are constrained; there is no generic arbitrary-URL verifier.
- Customer evidence is minimized before being written into receipts.
- Receipts are hashed and signed with Ed25519.
- Each receipt carries its signing key for integrity checks; issuer authenticity requires customers to pin the expected DoneProof public key out-of-band.
- Workspace records are tenant-scoped and contract IDs are immutable.

See [docs/SECURITY.md](docs/SECURITY.md) for the complete trust model and known pilot limitations.

## What DoneProof does not claim

DoneProof proves that configured postconditions matched independently observed evidence under the configured trust model. It does not prove causality, human intent, authorization, or that an external provider itself is truthful.

That boundary is deliberate. DoneProof is the **outcome assurance layer** between autonomous execution and business acceptance.

## Release status

`0.9.4` is intended for controlled design-partner and paid-pilot evaluation. The verification model, signed receipts, tenant isolation, durable PostgreSQL persistence, serverless readiness diagnostics and core provider paths are implemented. The remaining work toward managed enterprise GA is primarily managed OAuth, HA/operational scaling, KMS/HSM signing, SSO/RBAC, billing and additional native connectors.

## Validate locally

```bash
pytest -q
python -m compileall doneproof
python benchmarks/benchmark_core.py
```
