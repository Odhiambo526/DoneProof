# DoneProof v0.2

**Your agent says “done.” DoneProof proves it.**

DoneProof turns a human task into a **completion contract**, independently discovers/observes the target system, evaluates deterministic postconditions, and emits a tamper-evident verification receipt.

## Core invariant

> A task cannot become `VERIFIED` unless every required postcondition is independently observed and passes.

The executor's claim is never treated as evidence.

## Architecture

```text
human intent
   ↓
Astra contract compiler
   ↓
completion contract + task_started_at
   ↓
provider observer/discovery (GitHub now; Gmail next)
   ↓
deterministic predicates
   ↓
VERIFIED / PARTIAL / FAILED / UNKNOWN
   ↓
HMAC-signed evidence receipt
```

The LLM is deliberately **not** the source of truth. GPT-6 Astra translates ambiguous intent into explicit postconditions. Provider adapters query external state; deterministic predicates decide PASS/FAIL whenever possible.

## What v0.2 adds

A new GitHub resource can be verified **without trusting the agent to report its issue/PR number**.

Given:

```json
{
  "repo": "acme/api",
  "kind": "issue",
  "number": null,
  "title": "Auth bypass"
}
```

DoneProof searches resources created after the contract's `task_started_at` boundary.

- zero matches → the requested existence predicate fails
- one match → DoneProof re-fetches the canonical resource and verifies it
- multiple matches → `UNKNOWN`; DoneProof refuses to guess
- pre-existing matching resources → ignored
- GitHub 404 where absence/private inaccessibility cannot be distinguished → `UNKNOWN`

This closes a key trust gap: an execution agent cannot simply hand the verifier a convenient resource ID and call it proof.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\\Scripts\\activate
pip install -e '.[dev]'
cp .env.example .env
uvicorn doneproof.app:app --reload
```

Open `http://127.0.0.1:8000/docs`.

Run the built-in failure demo:

```bash
curl -s -X POST http://127.0.0.1:8000/v1/verify/demo | python -m json.tool
```

Expected verdict: `PARTIAL`. The requested title is correct, but the assignee is missing.

## Compile a contract with GPT-6 Astra

Set `OPENAI_API_KEY`, then:

```bash
curl -s http://127.0.0.1:8000/v1/contracts/compile \
  -H 'content-type: application/json' \
  -d '{
    "task":"Create GitHub issue Auth bypass in acme/api and assign alice",
    "task_started_at":"2026-09-04T03:00:00Z",
    "context":{"repo":"acme/api","title":"Auth bypass","assignee":"alice"}
  }'
```

The compiler defaults to `gpt-6-astra` and uses Structured Outputs. If the final GitHub number is unknown but safe discovery constraints exist, it emits `number: null` instead of downgrading immediately to `unresolved`.

## Verify real GitHub state

Set `GITHUB_TOKEN` for private repositories and higher rate limits. POST a completion contract to `/v1/verify`.

Known-number example: `examples/github_issue_contract.json`.

Discovery example: `examples/github_discovery_contract.json`.

```bash
curl -s -X POST http://127.0.0.1:8000/v1/verify \
  -H 'content-type: application/json' \
  --data-binary @examples/github_discovery_contract.json | python -m json.tool
```

## Receipt model

Each receipt contains:

- original task and contract id
- condition-level PASS / FAIL / UNKNOWN
- exact selector used, including injected task-time bound
- observed value and source URL
- provider notes / ambiguity evidence
- overall verdict
- SHA-256 receipt hash
- HMAC-SHA256 signature

HMAC is a v0.x tamper-evidence mechanism only. Production should use asymmetric signing keys in KMS/HSM so third parties can verify receipts without knowing the signing secret.

## Verdict semantics

- `VERIFIED`: every required postcondition passed.
- `PARTIAL`: some required outcomes passed and at least one failed, or optional conditions were not fully satisfied.
- `FAILED`: required postconditions failed and none passed.
- `UNKNOWN`: required state could not be independently and uniquely established.

## Security decisions

- Repository selectors are validated before constructing GitHub API paths.
- The adapter only calls `api.github.com`; there is no generic URL-fetch verifier/SSRF primitive.
- Redirects are disabled.
- Discovery is bounded by `task_started_at`, preventing a pre-existing resource from proving a new action.
- Duplicate discovery candidates produce `UNKNOWN`; the verifier never picks the most convenient match.
- GitHub 404 is treated conservatively because GitHub can conceal private resources behind 404.
- Model-generated selectors are validated; missing identity constraints cannot manufacture a successful lookup.
- External exceptions and inaccessible state map to `UNKNOWN`, never success.
- Verification evidence remains independent from the execution agent's claim.

## Tests

```bash
pytest -q
```

The suite covers deterministic verdicts, compiler schema safety, unique discovery, wrong-resource creation, duplicate-title ambiguity, task-time boundaries and privacy-preserving GitHub 404 behavior.

## Next

Day 3 is the Gmail verifier: independently distinguish **Sent** from **Draft**, then verify recipients, subject, thread and attachment metadata. See `ROADMAP.md`.
