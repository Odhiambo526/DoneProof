# Industry pilot guide

A DoneProof pilot should test one question:

> **How often does an agent-reported success differ from independently verified business state?**

## Choose the right workflow

Start with a workflow where false completion creates measurable cost and where authoritative evidence already exists.

Good examples:

- support refunds or account changes
- outbound invoice/report delivery
- CRM or ticket updates
- recruiting/application submissions
- GitHub engineering workflows
- approval workflows
- browser/RPA actions currently accepted from UI success messages

Avoid starting with subjective tasks such as “write a good reply.” DoneProof is strongest when success can be expressed as external state.

## Recommended experiment

Use 100–1,000 live or replayed tasks.

For every task record:

- executor-reported outcome
- DoneProof verdict
- failed/unknown postcondition
- verification latency
- whether the task required human review or repair

Primary metrics:

| Metric | Why it matters |
|---|---|
| False acceptance prevented | Claimed-success tasks that were not actually complete. |
| Verified rate | How many tasks met every required condition. |
| `UNKNOWN` rate | Where evidence quality or connectivity is insufficient. |
| Repairable failure rate | Failures an orchestrator can fix automatically. |
| Verification latency | Operational overhead introduced by assurance. |
| Cost per verified outcome | Commercial viability at production volume. |

## Pilot stages

### 1. Observe only

DoneProof verifies after execution but does not influence workflow decisions.

### 2. Gate acceptance

Only `VERIFIED` outcomes proceed automatically. Other verdicts go to repair or review.

### 3. Repair and reverify

The orchestrator uses failed postconditions to attempt targeted remediation, then asks DoneProof to verify again.

DoneProof should remain independent from the repair agent.

## Minimum pilot controls

1. Use registered runs for live tasks.
2. Keep evidence credentials separate from executor credentials where practical.
3. Use deterministic required postconditions.
4. Use `require_change=true` for important updates to pre-existing resources.
5. Export signed evidence bundles for incident review.
6. Define operational handling for `PARTIAL`, `FAILED` and `UNKNOWN` before gating production actions.

## Pilot exit decision

A pilot is commercially interesting when DoneProof prevents enough false acceptance, reduces manual checking, or improves auditability to justify its verification overhead.

Do not optimize the pilot around model benchmark scores. Optimize it around business outcomes that would otherwise have been accepted incorrectly.
