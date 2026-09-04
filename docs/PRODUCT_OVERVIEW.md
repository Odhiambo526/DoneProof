# DoneProof product overview

## One sentence

**DoneProof independently verifies that an AI agent produced the requested real-world system outcome and returns signed evidence of the result.**

## Why it exists

Agent platforms already provide traces, tool-call logs, evaluations and monitoring. Those are useful for understanding agent behavior, but they do not necessarily establish that the destination system ended in the requested state.

DoneProof verifies the destination state itself.

Examples:

| Agent says | DoneProof checks |
|---|---|
| “Email sent.” | Does a matching message exist in `SENT`, with the expected recipient and attachment? |
| “Issue created and assigned.” | Does the new issue exist, and is the expected assignee actually present? |
| “Refund completed.” | Did the authoritative payment or ERP system emit evidence that the refund reached the required state? |
| “Record updated.” | Did the authoritative record transition from the previous state to the requested state? |

## Where DoneProof sits

```text
Requester
   │ defines outcome
   ▼
DoneProof ───── registers assurance boundary
   │
   ├────────── Agent / RPA / automation executes
   │
   ▼
Authoritative business system
   │
   ▼
DoneProof independently observes state
   │
   ▼
Signed verification receipt
   │
   ├── VERIFIED → accept automatically
   └── PARTIAL / FAILED / UNKNOWN → repair or review
```

DoneProof does not need to be the agent runtime and does not depend on one model vendor.

## Best initial use cases

Start where a false “success” causes operational cost:

- refunds, credits and account changes
- outbound email and document delivery
- CRM or support-record updates
- recruiting/application workflows
- ticketing and approval workflows
- GitHub engineering automation
- browser/RPA workflows where a UI success state is not reliable proof

## Value to a customer

DoneProof can help a team measure and reduce **false acceptance**: tasks accepted as complete even though the external outcome was missing, incomplete or ambiguous.

A pilot should answer:

1. How often does the executor report success?
2. How often is that success independently `VERIFIED`?
3. What percentage would otherwise have been falsely accepted?
4. Which failed postconditions can be repaired automatically?
5. What does independent verification cost in latency and infrastructure?

## Not another observability platform

Observability asks: **What did the agent do?**

Evaluation asks: **How good was the agent's behavior or output?**

Governance asks: **What is the agent allowed to do?**

DoneProof asks: **What externally verifiable outcome became true?**

The categories are complementary rather than mutually exclusive.
