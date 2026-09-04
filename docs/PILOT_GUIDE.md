# Industry pilot guide

DoneProof pilots should start with a workflow where false completion has a visible operational cost.

Strong pilot candidates include:

- customer-support refunds or account changes
- recruiting/application workflows
- finance operations and invoice delivery
- CRM record creation or update
- GitHub engineering workflows
- internal ticketing and approval flows
- browser/RPA automations that currently rely on screenshots or success banners

## Recommended experiment

Select 100–1,000 real or replayed agent tasks and classify each outcome independently using existing business records. Run the same tasks through DoneProof and compare:

- executor-reported success rate
- DoneProof `VERIFIED` rate
- false-positive completion rate
- ambiguous/`UNKNOWN` rate
- automatic recovery opportunity
- verification latency
- cost per verified outcome

The most important metric is not model accuracy. It is **false acceptance prevented**: cases where an executor claimed success but required external state did not satisfy the completion contract.

## Pilot acceptance criteria

A useful pilot should demonstrate:

1. at least one workflow with independent external evidence
2. registered-run timestamps established before execution
3. no execution-agent access to evidence credentials where feasible
4. deterministic required postconditions
5. signed receipt export for audit or incident review
6. a documented handling path for `PARTIAL`, `FAILED`, and `UNKNOWN`

## Suggested rollout

### Observe only

DoneProof verifies outcomes but does not affect execution.

### Gate acceptance

Downstream systems accept `VERIFIED` tasks automatically and route other verdicts for repair or human review.

### Repair loop

An orchestrator receives failed postconditions and attempts targeted remediation, then asks DoneProof to verify again.

DoneProof itself should remain independent of the repair agent.
