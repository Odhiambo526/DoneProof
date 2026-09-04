# Architecture and trust model

DoneProof is intentionally small at the center. The verification engine does not need to reason like an agent; it needs to compare explicit postconditions with independently observed state.

## High-level flow

```text
              ┌──────────────────────────┐
              │ 1. Desired business      │
              │    outcome               │
              └────────────┬─────────────┘
                           │
                           ▼
              ┌──────────────────────────┐
              │ 2. Completion contract   │
              │    required conditions   │
              └────────────┬─────────────┘
                           │
                  register before action
                           │
                           ▼
              ┌──────────────────────────┐
              │ 3. Agent / automation    │
              │    executes              │
              └────────────┬─────────────┘
                           │
                           ▼
          ┌───────────────────────────────────┐
          │ 4. Authoritative external systems │
          │ GitHub · Gmail · ERP/CRM/webhook │
          └────────────────┬──────────────────┘
                           │ independent read
                           ▼
              ┌──────────────────────────┐
              │ 5. Verification engine   │
              │ deterministic predicates│
              └────────────┬─────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
      VERIFIED       PARTIAL/FAILED      UNKNOWN
          │                │                │
          └────────────────┼────────────────┘
                           ▼
              ┌──────────────────────────┐
              │ 6. Signed evidence       │
              │    receipt               │
              └──────────────────────────┘
```

## Components

### Completion contract

A small machine-readable definition of what must be true. It contains:

- the requested task
- one or more postconditions
- the evidence provider for each condition
- a deterministic predicate
- whether a condition is required
- whether proof of a state transition is required

The optional model compiler helps create this contract from natural language. It is not the verifier.

### Registration layer

For the strongest assurance mode, the contract is registered **before** execution. DoneProof assigns the contract ID and trusted start time.

For an update to an existing object, `require_change=true` captures the pre-execution predicate state. The final receipt can therefore distinguish:

```text
already true before execution   ≠   changed from false to true during the run
```

### Provider adapters

Adapters normalize authoritative state into small, stable fields. Current adapters are GitHub, Gmail and trusted webhooks.

Adapters are deliberately constrained. DoneProof does not expose a generic URL-fetching verifier because that would weaken the trust model and create an SSRF surface.

### Predicate engine

The engine evaluates deterministic operations such as:

```text
eq
neq
exists
not_exists
contains
contains_all
gte
lte
```

When a provider is inaccessible or evidence is ambiguous, the condition becomes `UNKNOWN` rather than being guessed.

### Receipt signer

Each receipt includes:

- exact completion-contract hash
- condition results
- minimized observed values
- provider references
- timing
- overall verdict
- receipt hash
- Ed25519 public key and signature

The receipt is self-contained for **integrity verification** because the signing public key is embedded in the signed record. That embedded key is not, by itself, proof of issuer identity: customers should pin the expected DoneProof public key through an independent channel. Historical receipts remain verifiable after rotation when the corresponding previously trusted key is retained.

## Assurance levels

### Registered

Recommended for live workflows. DoneProof establishes the timing boundary before execution and can capture transition baselines.

### Submitted

Useful for imports, retrospective checks and experiments. The caller supplies the contract at verification time, so temporal claims are weaker.

## Trust boundary

DoneProof establishes that **configured conditions match observed evidence**. It does not establish that:

- the agent caused the change rather than another actor
- the requester was authorized to request it
- the human instruction was interpreted correctly
- an upstream authoritative provider is itself truthful

For stronger deployments, keep evidence credentials and webhook secrets outside the executor's permissions and place DoneProof behind an API gateway with independent service identity.
