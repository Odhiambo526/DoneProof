# Managed real-world assurance plan

Base: RC PR #12, adb8cb19ff0e9956aff617893f63754cfb7cc8ae. The stack remains unmerged.

1. Strengthen GitHub App identity/installation validation and provide tenant-admin repository access discovery. Keep user authorization separate from executor credentials and preserve encrypted refresh/revision handling.
2. Add a resumable GitHub/Gmail rehearsal client against the deployed HTTP API and durable worker. Require PostgreSQL, operator-pinned issuer keys, trusted session preparation, negative receipts, external action, independent linked re-verification, and strict transition checks. Never execute the repair in DoneProof.
3. Harden provider resource identity and Gmail label interpretation. Support new-message sending through bounded discovery across changing Gmail message IDs; do not pretend a draft message ID is a stable sent-message identity.
4. Make registered preparation, transition proof, safe evidence/reasons, connection permissions and receipt history visible in the console/API. Add derived assurance metadata without changing receipt serialization/signatures.
5. Exercise replay, permission/refresh loss, no-change, rotation, worker checkpoint/restart and immutable linkage. Reuse existing PostgreSQL state machines and additive schema 7; avoid a new parallel execution engine.
6. Run full automated validation and all available real-provider rehearsals. Publish exact evidence and a gate-based readiness score; missing provider consent or persistent-host access remains an explicit blocker, never a passing fixture result.

Initial findings: managed OAuth and baseline/recovery machinery already exist. GitHub installation health does not bind returned installations to the configured app or expose authorized repositories. The console displays only the coarse receipt assurance field. Gmail draft sending changes message IDs. Live controlled OAuth/host configuration has been requested; no credentials are to be pasted into this task.
