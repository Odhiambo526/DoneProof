# Managed provider rehearsal

This change is based on RC PR #12. It is not a production rollout. Use the existing release gates in `RELEASE_CANDIDATE_REPORT.md` (and the deployment runbooks); a green fixture test is not provider consent or a persistent hosted worker.

## Connect independently

Configure the trusted server's `DONEPROOF_PUBLIC_URL`, connection-admin tenant mapping, AES-GCM key ring/active key, and OAuth client secrets through the deployment secret store. Neither SDK nor executor receives these secrets. API and workers must share PostgreSQL, the encryption key ring and a stable issuer signing seed. Keep old public signing keys pinned after rotation; changing the issuer seed must not rewrite receipts.

GitHub: use a GitHub App with read-only Issues, Pull requests and Metadata permissions. Enable expiring user authorization tokens and the server callback `/v1/connections/oauth/github/callback`. Configure `DONEPROOF_GITHUB_CLIENT_ID`, `DONEPROOF_GITHUB_CLIENT_SECRET`, and `DONEPROOF_GITHUB_APP_SLUG`. Install that app on the selected private repository, then authorize it from Connection Settings as the tenant administrator. DoneProof checks the configured app identity, suspension and read-only installation permissions. This is a GitHub App **user OAuth** flow, not an installation-token/JWT flow. Access is the intersection of the consenting user's access and the app installation. No executor token is imported.

`GET /v1/connections/{id}/repositories` lists authorized repositories for the current managed connection. It is tenant-admin-only, revision-checked, bounded to 100 installations and 500 repositories, and refuses incomplete or malformed responses. This list is planning metadata, never verification evidence. Installations of unrelated apps cannot establish access.

Gmail: configure `DONEPROOF_GOOGLE_CLIENT_ID` and `DONEPROOF_GOOGLE_CLIENT_SECRET` with callback `/v1/connections/oauth/gmail/callback`. Use the existing Gmail read-only scope and server-side encrypted refresh flow. Google OAuth consent/verification requirements remain an external release gate. Sending is the external agent's responsibility. Provider token expiry, refresh races, revision changes and disconnects retain the existing fail-closed lifecycle.

## Prepare first, act externally, verify durably

The console now offers **Prepare before your agent acts**, explicit false-to-true transition policy, resumable session IDs, durable verification, remediation and linked re-verification. Enter a separately trusted issuer public key to check receipt signatures. A displayed issuer key is not automatically trusted. Browser evidence remains visibly lower assurance and there is no API-to-browser fallback.

Python:

```python
session = dp.assurance.prepare(
    task="Close issue #123 in owner/repository",
    idempotency_key="customer-outcome-123",
    require_transition=True,
)
# Continue only for READY_FOR_EXECUTION; handle clarification first.
# Your existing agent acts externally here.
result = dp.assurance.verify(session.id, wait=True)
# Repair externally if needed, then reverify with the prior receipt ID
# and a stable, new repair-operation idempotency key.
```

`require_transition` defaults to false for compatibility. When true, every required condition must have a trusted false baseline and a later true observation to certify it. Already-true state cannot pass that policy. Derived `session.assurance` and `GET /v1/receipts/{id}/assurance` distinguish submitted, registered and transition-assured results without altering signed receipt fields or bytes. The derived summary is not an additional signed claim; independently check the signed transition fields and the pinned signature.

Both SDKs now wait through PREPARING after a slow/lost preparation response, polling the original session within the original absolute deadline. They do not recapture a baseline. Existing preparation idempotency hashes with the default policy remain compatible.

## Reusable real-provider CLI

Set `DONEPROOF_API_KEY` securely and save a trusted public-key map as `pins.json`. Use a dedicated controlled issue identified at runtime, not a hardcoded issue number:

```sh
python scripts/rehearse_provider.py prepare --provider github --repo owner/repository --issue 123 --base-url https://staging.example --pins pins.json --state rehearsal.json
python scripts/rehearse_provider.py negative --base-url https://staging.example --pins pins.json --state rehearsal.json
# Close the issue using the separate external actor.
python scripts/rehearse_provider.py positive --base-url https://staging.example --pins pins.json --state rehearsal.json
```

The tool requires PostgreSQL and stable staging/production signing, rejects UNKNOWN/inaccessible/stale evidence, checks exact target/predicate binding, verifies pinned signatures, requires false-to-true transitions and immutable linked receipts, and can resume its stable identity after a lost response. A `.lock` file prevents concurrent state replacement. After a crash, verify that the recorded process is no longer running before removing only that lock and resuming the same state file. A clarification requires a new outcome identity after resolving the cause; never overwrite a completed preparation to recapture its baseline.

For Gmail, prepare with `--provider gmail --to controlled@example.com --subject-prefix "DoneProof rehearsal"`. The tool adds a unique suffix and prints the exact subject. Prepare before creating/sending the message. Run the negative step while no sent message exists (a draft is acceptable), send externally, then run positive. Do not use the original draft message ID: Gmail deletes the draft and creates a new sent-message ID. Duplicate matches, incomplete searches, contradictory DRAFT/SENT labels and unreadable candidates are UNKNOWN. See [Google's draft lifecycle](https://developers.google.com/workspace/gmail/api/guides/drafts).

## Deployment compatibility

No database migration is added; migrations 1–7 and receipt schemas 1.0/1.1/1.2 are preserved. GitHub/Gmail declaration versions move to 1.0.1 because their validation semantics are stricter. Drain old workers and register new runs after upgrading the fleet. Old provider bindings fail closed; existing signed receipts remain verifiable with their original pins. This is an intentional compatibility boundary, not a silent rebind.

The managed durable HTTP response hook now runs only after bounded body consumption. This closes a bypass where the hook could read an entire provider response before the size guard. Provider error bodies and credentials are not included in exception messages.

The compiler corpus retains the real Gmail tasks but corrects three golden labels that incorrectly treated a draft ID as a stable sent-message selector. The compiler now requests identifiers for an independently discoverable sent outcome. The resulting lower valid-contract count is intentional provider correctness, not reduced UNKNOWN safeguards.

The live restart rehearsal also exposed a current-schema startup deadlock: replaying migration DDL acquired exclusive table locks while the API was using verification tables. Current schema startup now validates the contiguous ledger and synchronizes slots without replaying DDL. Incomplete/future ledgers fail closed. Upgrade fixtures now represent contiguous historical ledgers rather than impossible missing-middle histories. Worker startup errors exit with a fixed, secret-free message; the deployment supervisor remains responsible for restarting a process after database/configuration recovery.

If a verification expires during a worker outage, keep that failed job in history. After restoring the worker, explicitly choose a new `--repair-attempt 2` for the positive step. The server enforces terminal-state/race rules and the maximum attempt count. Reusing the same attempt resumes the same job; the tool never silently replaces a failed attempt or changes its trusted baseline.
