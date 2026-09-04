# Python client

DoneProof includes a small synchronous client for pilot integrations.

```python
from doneproof.client import DoneProofClient

contract = {
    "task": "Create issue Auth bypass and assign alice",
    "postconditions": [
        {
            "id": "issue-title",
            "description": "Requested issue exists",
            "provider": "github",
            "selector": {"repo": "acme/api", "kind": "issue", "number": None, "title": "Auth bypass"},
            "predicate": {"op": "eq", "path": "title", "expected": "Auth bypass"},
            "required": True,
        }
    ],
}

with DoneProofClient("https://doneproof.example", api_key="dp_live_acme") as dp:
    run = dp.register_run(contract)
    # Existing agent executes here.
    receipt = dp.verify_run(run["id"], idempotency_key="agent-task-1842")
    bundle = dp.evidence_bundle(receipt["receipt_id"])
```

For high-assurance workflows, prefer `register_run()` followed by `verify_run()` so DoneProof establishes the task boundary before execution.
