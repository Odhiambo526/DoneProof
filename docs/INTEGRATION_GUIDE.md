# Integration guide

For live workflows, use the registered-run lifecycle. It gives DoneProof a trusted timing boundary before the executor acts.

## Authentication

Map API keys to workspace IDs:

```bash
DONEPROOF_API_KEYS_JSON='{"dp_live_acme":"acme","dp_live_beta":"beta"}'
```

Clients send:

```text
X-DoneProof-Key: dp_live_acme
```

If no keys are configured, DoneProof runs in development/open mode. Production mode refuses to start without workspace authentication.

## 1. Register a run

```text
POST /v1/runs
```

DoneProof:

1. assigns a new contract ID
2. replaces caller timing with server time
3. stores the immutable contract
4. captures pre-execution baselines for `require_change=true` conditions

Wait for this response before starting the executor when using transition assurance.

## 2. Execute normally

No DoneProof SDK is required inside the agent itself. The executor can be any model, agent framework, browser automation, RPA system or internal service.

## 3. Verify

```text
POST /v1/runs/{contract_id}/verify
```

Use an `Idempotency-Key` when retries are possible.

## GitHub

Known issue:

```json
{
  "provider": "github",
  "selector": {"repo":"acme/api","kind":"issue","number":77},
  "predicate": {"op":"contains","path":"assignees","expected":"alice"}
}
```

New resource with unknown number:

```json
{
  "provider": "github",
  "selector": {
    "repo":"acme/api",
    "kind":"issue",
    "number":null,
    "title":"Auth bypass"
  },
  "predicate":{"op":"eq","path":"title","expected":"Auth bypass"}
}
```

DoneProof only searches resources created after the registered run began. Multiple candidates return `UNKNOWN`.

## Gmail

Configure a default mailbox token:

```bash
GMAIL_ACCESS_TOKEN=...
```

or tenant-specific tokens:

```bash
DONEPROOF_GMAIL_TOKENS_JSON='{"acme":"ya29..."}'
```

A send task should verify `location == sent`:

```json
{
  "provider":"gmail",
  "selector":{
    "message_id":null,
    "subject":"September invoice",
    "to":"finance@example.com"
  },
  "predicate":{"op":"eq","path":"location","expected":"sent"}
}
```

Normalized fields include:

```text
message_id, thread_id, location, subject, from, to, cc, bcc,
internal_date, attachment_names
```

## Trusted webhook evidence

Use webhooks when the authoritative state lives in a customer system without a native DoneProof adapter.

Configure a source:

```bash
DONEPROOF_WEBHOOK_SOURCES_JSON='{
  "erp": {"tenant":"acme","secret":"replace-with-secret"}
}'
```

Send evidence to:

```text
POST /v1/webhooks/erp
```

Headers:

```text
X-DoneProof-Timestamp: <unix-seconds>
X-DoneProof-Event: refund.completed
X-DoneProof-Object-ID: refund-42
X-DoneProof-Signature: sha256=<hex-hmac>
```

Signature input:

```text
{timestamp}.{event_type}.{object_id}.{raw_json_body}
```

Contract condition:

```json
{
  "provider":"webhook",
  "selector":{
    "source":"erp",
    "event_type":"refund.completed",
    "object_id":"refund-42"
  },
  "predicate":{
    "op":"eq",
    "path":"payload.status",
    "expected":"completed"
  }
}
```

The webhook credential should belong to the authoritative system and should not be available to the executor.

## Optional natural-language compiler

```text
POST /v1/contracts/compile
```

The compiler translates human intent into explicit postconditions. It does not decide the verification verdict. If no model credential is configured, explicit contracts remain fully functional.

## Submitted verification

```text
POST /v1/verify
```

Use this for retrospective imports and experiments. It returns `assurance_level="submitted"` because DoneProof did not establish the timing boundary before execution.

## Batch verification

```text
POST /v1/verify/batch
```

Useful for replay studies and pilot datasets. It is not a substitute for registered runs in live high-assurance workflows.

## Receipts and audit

```text
GET /v1/receipts
GET /v1/receipts/{receipt_id}
GET /v1/receipts/{receipt_id}/integrity
GET /v1/receipts/{receipt_id}/bundle
GET /v1/audit
GET /v1/overview
```
