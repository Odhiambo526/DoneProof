# FAQ

## Why isn't this just an agent feature?

An agent vendor can improve its own success checks. DoneProof is useful when the organization wants one independent assurance layer across multiple executors and business systems.

The executor is optimized to complete work. DoneProof is optimized to refuse certification when evidence is insufficient.

## How is DoneProof different from agent observability?

Observability records traces, model calls, tool calls and execution behavior. DoneProof reads the destination system and verifies explicit business outcomes. A customer can use both.

## How is it different from agent evaluation?

Evaluation scores agent quality on datasets or production interactions. DoneProof produces a per-action evidence-backed verdict against external state.

## Does DoneProof need access to chain-of-thought?

No. It does not require private model reasoning. The relevant inputs are the desired outcome, the completion contract and authoritative external evidence.

## Does DoneProof need to execute the task?

No. Separation from execution is a feature. Any agent, model, RPA system or conventional automation can perform the action.

## Can DoneProof guarantee the agent caused the outcome?

No. It proves that the required state or registered transition was observed after the assurance boundary. Causal attribution requires additional identity or provider-level event evidence.

## Why is `UNKNOWN` useful?

Because inaccessible or ambiguous evidence should not become false certainty. `UNKNOWN` means the system cannot safely establish the requested state and should escalate, retry or obtain better evidence.

## Is DoneProof ready for enterprise production?

The current release is designed for controlled pilots and design partners. The assurance engine and core trust model are implemented. Managed enterprise GA still requires infrastructure such as production OAuth, HA storage, KMS/HSM signing, SSO/RBAC and organization administration.
