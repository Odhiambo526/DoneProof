# Phase 7 adversarial review

The review treats SDK convenience and session metadata as untrusted planning
information. Only existing provider observations and signed receipts establish
outcomes. Tests use local fixtures and configured mock providers.

| Failure hypothesis | Evidence / disposition |
| --- | --- |
| Double preparation captures a later boundary | Concurrent preparation test admits one reservation and one baseline capture. Expired reservations remain failed under the same key. |
| Duplicate verify or lost HTTP response creates duplicate jobs | Session transaction serializes initial admission; different keys converge. SDK lost-response tests replay identical body/key/request ID and assert one job. |
| Session ID crosses tenants | GET, verify, reverify and receipt tests return 404 for the other tenant. Every store lookup includes tenant. |
| Changed provider declaration is accepted | Session fingerprint admission rejects changed declarations. Existing provider-registry job/worker tests retain declaration/version fencing at observation and publication. |
| Expired connection is silently treated as available | Existing managed-connection refresh/expiry tests remain in the full suite. Session preparation with UNKNOWN baseline returns clarification without a registered contract. |
| Worker restart changes evidence or signs twice | Sessions use the unchanged durable jobs and publication transaction. Existing crash, lease-stealing, observation checkpoint, signing handoff and immutable-publication tests run in the full suite. |
| Callback duplication/replay or event-header substitution is accepted | Both SDKs verify raw HMAC bytes, bounded skew and body/header/job identity before atomic deduplication. An actual worker-produced callback is accepted by the Python helper; tampering/replay is rejected. |
| Repair races initial verification or advances the wrong head | Session recovery refuses incomplete initial jobs and requires previous_receipt_id. Existing recovery lock and max-attempt tests remain authoritative. |
| Cancellation credits an earlier receipt as the new result | Session state becomes UNKNOWN without a current receipt/verdict after cancelled recovery; history remains intact. Existing cancel-versus-sign publication tests run unchanged. Local SDK cancellation sends no implicit server mutation and interrupts in-flight waiting. |
| Malformed compiler output yields a ready session | Inconsistent valid-contract/clarification output permanently fails preparation. Both SDKs reject malformed ready/clarification results and unsupported protocol versions. |
| Browser evidence is rendered like an API | Browser sessions expose lower assurance and provenance. Schema downgrade and classification mismatch tests reject invalid responses. No fallback path was added. |
| Executor injects screenshots, observations, success or reasoning | Closed session request models reject these fields at the top level and in undeclared context. Scheduling input accepts only deadline/callback/previous receipt options. Existing guidance-as-evidence rejection tests remain in the full suite. |
| Exceptions/hooks disclose secrets | Tests inject sentinel secrets in transport failures and response bodies; exported exceptions/hooks retain fixed codes and metadata only. Callback receiver backend failures are sanitized. |
| Retrying disconnect disables a newly reconnected account | Added revision-bound v2 disconnect with atomic operation keys. Test performs mocked browser OAuth reconnect, replays the original disconnect and proves the new connection remains connected. |
| JS receipt parsing silently rounds signed numbers | Exact signed bytes are verified rather than reserialized. The Node SDK additionally rejects integers outside its safe range, preventing misleading rounded observations. |

## Intentional limits

- A process that dies during preparation does not resume baseline capture; the
  failed operation needs a new session key before external execution. A concurrent
  request may observe PREPARING and should fetch that session until resolved.
- Compiler v2 retains its existing 50-condition contract limit; the existing
  low-level durable job API continues to support 1,000-condition workloads.
- Initial jobs that expire or fail internally without a signed receipt are not
  automatically replaced. Explicit low-level resubmission can retain the
  original registered boundary but is not attached to the original session.
- MemoryReplayStore is development-only. Production receiver deduplication must
  use an atomic durable shared inbox, with processing recoverable after claim.
- A Node receipt containing an integer outside the safe range is unsupported;
  Python can retain those values. Browser execution is never used as fallback.
- Connection onboarding deliberately requires the trusted browser settings flow
  and administrator interaction. SDKs do not transport its binding cookie.
- The fixture demos and framework recipes do not claim live external-provider
  or current third-party framework runtime validation.

The first local Chromium-inclusive full run encountered the existing SQLite
provider concurrency test's exact-call-count assertion during concurrent heavy
host work (42 calls rather than 40; concurrency still bounded at 8). Its focused
rerun and a clean complete rerun passed, as did all CI matrix variants. The test
was not weakened and no retry or concurrency policy was changed.
