# Completion Contract Compiler v2

The compiler now checks whether a requested outcome can be independently verified before
publishing an executable contract. Compilation has seven stages: intent decomposition,
workspace capability resolution, candidate construction, deterministic static validation,
selector resolution, ambiguity assessment, and final contract construction.

This changes planning, not evidence. Executor claims and model confidence do not enter
provider observations, baselines, verdicts or signed receipts. Contract schemas and receipt
schemas remain version 1.0; the new compilation response is version 2.0.

## API

`POST /v2/contracts/compile` requires the existing `X-DoneProof-Key` workspace credential:

```json
{
  "task": "Close issue #42 in acme/api; Send email to ana@example.com with subject \"Issue 42 resolved\"",
  "context": {}
}
```

The response contains `status`, nullable `contract`, `clarification_requirements`,
`contract_quality`, `selector_checks`, completed `stages`, `deterministic`, `usage` and
`latency_ms`. HTTP 200 means compilation completed; clients **must inspect `status`**.
A failed required outcome prevents publication of the whole contract. Only successful
contracts are persisted, using the existing tenant-scoped storage and audit tables.

| Status | Meaning |
| --- | --- |
| `valid_contract` | All required conditions passed analysis and selector checks. |
| `unsupported_provider` | No supported authoritative integration covers the task. |
| `missing_identifier` | Required identifiers/constraints are missing, ungrounded, or a resource was not found. |
| `ambiguous_resource` | Multiple resources or unresolved interpretations match. |
| `unverifiable_outcome` | The outcome, connection, predicate or model service cannot support authoritative verification yet. |

Clarification entries contain stable `code`, `category`, `message`, `condition_ids` and
`fields`. For example, `missing_identifier` identifies the fields the caller must supply;
`provider_unavailable` asks the caller to connect/reconnect and retry. Provider bodies,
candidate resource lists, credentials and raw model errors are excluded. Inaccessible
GitHub resources retain indeterminate semantics; they are not treated as proven absence.

`GET /v2/contracts/capabilities` reports deterministic parsing, model configuration and
actual workspace connector availability. It does not enumerate another tenant's webhook
sources. Both routes use authentication, tenant rate limiting and `Cache-Control: no-store`.
Validation errors on v2 routes do not echo request values.

`POST /v1/contracts/compile` uses the same pipeline and retains the successful
`CompletionContract` response shape. Model unavailability remains HTTP 503; invalid
compilation remains HTTP 502 when the model is configured. Clients needing detailed
clarifications should use v2. Existing `/v1/capabilities` fields remain compatible;
the v2 capability route explicitly reports the deterministic path without a model key.
Existing verification, registered runs, durable jobs and receipt APIs retain their schemas.

## Deterministic parsing and grounding

Full-clause grammars cover issue/PR close, reopen, merge, lock, unlock, assignment, labels,
renaming, explicit state checks and creation; Gmail send, draft-to-sent transitions,
sent/draft checks and attachment metadata; and signed webhook events with exact object
identifiers and optional explicit payload equality. Semicolons or newlines separate clauses.
Semicolons within quoted values remain data. An optional `Please` prefix is normalized.
Unconsumed text, conditional requests and extra requirements never silently disappear.

Useful examples:

```text
Assign issue #42 in acme/api to maya
Create pull request in acme/api titled "Ship billing fix" from "fix/billing" to "main"
Send Gmail draft msg105
Check Gmail message with subject "Receipt 77" to ana@example.com is sent
Wait for webhook "refund.completed" from "erp" for object "order-42" with payload.status = "refunded"
```

Typed context can supply literal bindings: `repo`, `kind`, `number`, `title`, `author`,
`head_ref`, `base_ref`, `message_id`, `subject`, `to`, `thread_id`, `source`, `event_type`,
`object_id`, `assignee`, `label`, `attachment_name`. For example, `Close issue #42` with
`{"repo":"acme/api"}`, or `Send email` with exact `subject` and `to`, stays deterministic.
Context is intent input; it cannot establish observed success, connections or tenant identity.
Unknown context objects, including executor claims, are excluded from model input.

Each model intent must quote an ordered, verbatim task span. All task text and all
conditions must be covered, without optionalizing requested outcomes. IDs must occur as
literal values in the corresponding intent or typed context. The only exception is a new
ID returned by a successful authoritative selector lookup. Model confidence cannot waive
these checks. Predicate values must also be grounded or derived from recognized intent.

The seven static rule families reject duplicates, contradictory predicates, impossible
selectors, unsafe discovery, meaningless predicates, missing transitions and over-broad
postconditions. Analysis also validates provider field types, supported operations,
required conditions, exact-clause coverage and sensitive fields. Compiler v2 deliberately
does not emit root-existence or absence predicates, empty containment checks, unsupported
PR-review approvals, email body/read-receipt claims, or generic arbitrary-URL verification.

## Selector checks and execution

Existing resources use the same tenant-bound adapters as verification. Exact IDs are read
and checked; existing-resource discovery uses exact constraints and a bounded complete
search. A unique result is pinned to its provider ID. Multiple matches require an exact
identifier. Zero matches or an incomplete/inaccessible search prevents an executable
contract. Contradictions are checked again after pinning, including aliases of one resource.

New resource creation and future signed events report `deferred`. Their selector is
executable, but the future resource does not exist yet. GitHub/Gmail preflight checks
availability and ambiguity; trusted webhook sources are checked against the tenant's
configured ingestion source. Execution then discovers within the registered time boundary.
`deferred` is not counted as a successfully resolved existing resource in evaluation.

GitHub's five-page discovery limit and Gmail's 100-message search limit now return UNKNOWN
when search completeness cannot be established. Missing candidate details also fail closed.
These changes prevent incomplete searches from claiming absence or uniqueness.

Preflight creates no baseline and signs no receipt. Register the returned contract through
`/v1/runs` **before execution**, then use the registered verification/job flow. Registration
establishes server time and captures the account-bound baseline. Existing-resource mutations
require a false-to-true transition; directly submitting them still produces UNKNOWN without
that baseline. Provider state and connection identity are checked again during verification.

`contract_quality.confidence` is an uncalibrated structural planning score (0.95 for the
deterministic path, 0.8 for accepted model interpretation, 0 for failed compilation).
Its scope and lack of calibration are explicit in the object. Specific warnings explain
preflight limitations, future discovery and model interpretation. It is **not** a probability
of task completion and is never receipt evidence. Review model-interpreted intent before
registering it; static analysis cannot prove arbitrary natural-language entailment.

## Astra policy and operation

Configure `OPENAI_MODEL=gpt-6-astra` and, for language outside the deterministic grammar,
`OPENAI_API_KEY`. `DONEPROOF_COMPILER_REASONING_EFFORT` accepts `low` (default) or `medium`.
The first attempt uses that ordinary effort. Repairable validation failures or explicit
intent ambiguity permit `high`, then `xhigh`, with at most three model calls. Missing IDs,
unsupported integrations, unavailable connections, multiple authoritative resources and
HTTP failures cannot be fixed by guessing and do not trigger expensive escalation.

Requests use the fixed OpenAI Responses endpoint, strict structured output, `store:false`,
an 8,192-output-token cap, a 40-second model HTTP timeout and a 90-second whole-compilation
deadline. There are at most two active model calls per API process, four GitHub and two
Gmail preflight reads, with 15-second provider timeouts. These are process-local bounds;
aggregate capacity scales with API workers. The existing workspace rate limiter still applies.
Raw prompts, model errors, provider resource bodies and credentials are not added to logs,
audit records or quality responses. OpenAI receives the task, allowed literal context,
coarse workspace capabilities and fixed validation codes; no provider bodies or tokens.

Token usage includes input, cached input, cache writes, output and reasoning tokens across
all attempts. `usage.complete=false` means a failed request may have consumed tokens without
returning usage. Reasoning tokens are included in output usage and must not be billed twice.
No new PostgreSQL migration or data backfill is required. This stacks on Phases 1 and 2.

The requested effort values, structured output support and current pricing were verified
against [OpenAI's Astra model documentation](https://developers.openai.com/api/docs/models/gpt-6-astra).

## Evaluation and validation

The corpus contains **125 tasks: 44 GitHub, 41 Gmail and 40 webhook workflows**, including
39 negative cases, missing/ambiguous selectors, unavailable connectors, unsupported outcomes,
contradictions, extra requirements and credential sentinels. Expected conditions are specified
independently of the compiler in `evaluations/corpus.py`. The runner also violates each golden
condition individually and checks that the resulting receipt is not VERIFIED.

```bash
ruff check doneproof tests scripts evaluations
pytest -q
TEST_DATABASE_URL=postgresql://... pytest -q
docker build -t doneproof:compiler-v2 .
python evaluations/run_compiler.py --output evaluations/results/offline.json --export-corpus evaluations/results/tasks.jsonl
# Set OPENAI_API_KEY securely in the process environment before a live model evaluation.
python evaluations/run_compiler.py --mode live --output evaluations/results/live.json
```

Both evaluation modes use deterministic provider fixtures, not live Gmail/GitHub/webhook
traffic. Offline mode disables the model and measures the fail-closed fallback. Live mode
measures actual Astra responses, latency and token usage against the same golden corpus.
It requires an API key and incurs normal API charges. CI runs the complete corpus offline,
the full PostgreSQL 16 suite, Ruff, Python 3.11–3.13 tests, and Docker worker startup/shutdown.

The committed offline summary measures 79/125 valid contracts (63.2%), 79/86 valid-case recall
(91.86%), 32/40 existing-selector resolutions (80%), and 47 deferred selectors. All 39 negative
cases require clarification; seven additional valid tasks require model interpretation.
False-certifiable contracts: 0/79, including 158 negative-world checks. Unnecessary UNKNOWN:
0/79 exercised fixture verifications. Clarification rate: 46/125 (36.8%). No model calls means
zero tokens and zero token cost. Latency values describe the local fixture environment only.

Metric denominators are included in every report. False-certifiable counts include emitted
contracts missing golden requirements or permitting a golden counterexample to verify.
Unnecessary UNKNOWN counts accepted contracts whose complete fixture state still yields
UNKNOWN; compilation refusals are accounted for separately by clarification rate and recall.
The cost estimate uses dated standard short-context list prices, including cached/cache-write
input; it is not an invoice and is null when usage is incomplete.

No live Astra key was available during implementation. Live model quality, production
provider latency and billed model cost therefore remain unmeasured. Zero failures in this
finite offline corpus are regression evidence, not a universal correctness guarantee.
