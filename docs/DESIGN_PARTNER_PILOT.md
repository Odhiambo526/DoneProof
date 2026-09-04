# DoneProof Design Partner Pilot

## What the pilot answers

AI agents can report that work is complete even when the destination system does not reflect the required business outcome.

The DoneProof pilot measures that gap directly:

> **Of the tasks your agent reports as successful, how many are independently verifiable as complete?**

DoneProof runs outside the executor, observes authoritative destination state, evaluates explicit completion conditions, and issues a signed verification receipt.

## Pilot scope

A typical design-partner pilot covers **one production agent workflow for 30 days**.

Good candidates include workflows where a false "done" matters:

- customer follow-ups and outbound email;
- CRM, ATS, ticketing, or ERP updates;
- browser-agent submissions;
- support/refund actions;
- GitHub issue or pull-request automation;
- internal operational workflows that cross system boundaries.

DoneProof does not replace the agent, model, framework, or observability stack.

## What we measure

For each agreed task, DoneProof compares the expected outcome with independently observed evidence and returns one of four verdicts:

- `VERIFIED` — every required outcome is supported by authoritative evidence;
- `PARTIAL` — some required outcomes passed and others failed;
- `FAILED` — required outcomes failed and none passed;
- `UNKNOWN` — authoritative state could not be established safely.

The pilot report focuses on:

1. agent-reported success rate;
2. independently verified success rate;
3. false-completion rate;
4. recurring failure patterns;
5. verification latency and evidence coverage;
6. workflows where stronger acceptance gates would materially reduce operational risk.

## Integration

The preferred flow is:

```text
register expected outcome
        ↓
existing agent executes normally
        ↓
DoneProof independently observes destination state
        ↓
deterministic acceptance checks
        ↓
VERIFIED / PARTIAL / FAILED / UNKNOWN
        ↓
Ed25519-signed evidence receipt
```

Current native evidence paths include GitHub, Gmail, and signed webhooks for customer systems. Additional systems can be evaluated during pilot scoping.

## Deliverables

A design partner receives:

- founder-led integration support;
- a dedicated DoneProof workspace;
- signed machine-readable verification receipts;
- access to the assurance console and API;
- a final outcome-assurance report comparing reported vs verified success;
- a prioritized list of the failure classes worth addressing before broader autonomous deployment.

## Security boundary

DoneProof does not treat the executor's own success message as evidence.

Receipts bind to the exact completion contract, evidence and verdict, and are signed with Ed25519. DoneProof proves that configured postconditions matched independently observed evidence under the configured trust model; it does not claim to prove causality, human intent, authorization, or that an upstream provider is truthful.

See [Security model](SECURITY.md) and [Architecture](ARCHITECTURE.md) for the complete trust boundary.

## Evaluation goal

The pilot is successful if DoneProof gives the team a materially stronger answer to:

> **Did the agent actually finish the job?**

than the executor's own completion signal or trace currently provides.

## Production pilot

- Product: https://www.getdoneproof.com
- Assurance console: https://www.getdoneproof.com/console
- API reference: https://www.getdoneproof.com/docs

DoneProof `0.9.4` is currently intended for controlled design-partner and paid-pilot evaluation.
