# DoneProof pilot report template

## Executive summary

- Workflow evaluated:
- Agent/executor:
- Tasks evaluated:
- Pilot period:
- Authoritative evidence source:
- Executor-reported success rate:
- DoneProof verified rate:
- False acceptance prevented:
- `UNKNOWN` rate:
- Median verification latency:

## Outcome distribution

| Verdict | Count | Rate |
|---|---:|---:|
| VERIFIED |  |  |
| PARTIAL |  |  |
| FAILED |  |  |
| UNKNOWN |  |  |

## Most common incomplete outcomes

| Failed/unknown condition | Count | Operational impact | Repairable automatically? |
|---|---:|---|---|
|  |  |  |  |

## Business value

Estimate:

- manual checks avoided
- false completion incidents prevented
- repair actions that can be automated
- incremental verification latency
- cost per verified outcome

## Recommendation

Choose one:

- keep DoneProof in observe-only mode
- gate high-risk actions on `VERIFIED`
- add automated repair + reverify
- improve evidence quality before gating
- stop the pilot because false acceptance is too low to justify assurance overhead

The purpose of the pilot is to determine whether independent outcome verification creates measurable operational value, not to manufacture a positive result.
