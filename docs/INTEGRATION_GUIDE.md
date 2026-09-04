# Integration guide

## Authentication

Configure workspace API keys as a JSON mapping:

```bash
DONEPROOF_API_KEYS_JSON='{"dp_live_acme":"acme","dp_live_beta":"beta"}'
```

Clients send:

```text
X-DoneProof-Key: dp_live_acme
```

When no keys are configured, DoneProof runs in development/open mode. Production readiness fails if authentication is missing.

## High-assurance run lifecycle

### Register

`POST /v1/runs`

The request contains a completion contract. DoneProof stores it and replaces its timing boundary with server time.

### Execute

Your existing agent performs the action. DoneProof is executor-agnostic.

For a mutation of an existing resource, set `require_change: true` on the relevant postcondition before registering the run. DoneProof captures whether the predicate was already satisfied and only credits the executor when the final state proves an unsatisfied → satisfied transition.

### Verify

`POST /v1/runs/{contract_id}/verify`

Use an `Idempotency-Key` for retried calls. Reusing the same key for a different request returns `409`.

## GitHub

A known resource can use its number:

```json
{
  "provider": "github",
  "selector": {"repo":"acme/api","kind":"issue","number":77},
  "predicate": {"op":"contains","path":"assignees","expected":"alice"}
}
```

For newly created resources, omit the final number and supply safe discovery constraints:

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

DoneProof only searches resources created after the registered run began. Multiple matches return `UNKNOWN`.

## Gmail

Configure either a default access token or a tenant mapping:

```bash
GMAIL_ACCESS_TOKEN=...
```

or:

```bash
DONEPROOF_GMAIL_TOKENS_JSON='{"acme":"ya29..."}'
```

A send outcome should explicitly verify `location == sent`:

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

Useful normalized fields:

```text
message_id, thread_id, location, subject, from, to, cc, bcc,
internal_date, attachment_names
```

## Trusted webhooks

Webhooks are the generic enterprise integration path.

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

Required headers:

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

Example contract:

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

## Natural-language compiler

`POST /v1/contracts/compile` is optional. If `OPENAI_API_KEY` is not configured, explicit contracts remain fully functional.

The compiler's role is only to translate intent into postconditions. Provider observation and deterministic predicate evaluation remain separate.
