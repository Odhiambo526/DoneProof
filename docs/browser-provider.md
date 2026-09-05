# Browser evidence for UI-only workflows

`browser` is an SDK adapter loaded from the installed `doneproof.providers` entry point. It verifies an independently navigated UI in fresh, unauthenticated Chromium state. It never clicks controls, fills forms, accepts executor screenshots, loads executor profiles, borrows cookies, attaches to a browser, or performs a business action. Login-required UI, challenges and interstitials return **UNKNOWN**. There is no authentication bypass or arbitrary-URL verification endpoint.

Browser evidence is **lower assurance than an authoritative first-party API**, including in a registered run. Registration establishes a temporal boundary; it does not upgrade the trustworthiness of the evidence source. A VERIFIED browser receipt means the approved UI postcondition matched, not that an API or underlying business system confirmed it.

## Approve a UI check

Only deployment operators configure checks. First establish that the workflow has no authoritative API, including private APIs or trusted signed event sources. An expired or disconnected API connection does not justify browser fallback. Fix the API connection instead. The compiler only accepts explicit browser-check clauses and does not translate failed Gmail/GitHub/API tasks to browser selectors.

`DONEPROOF_BROWSER_CHECKS_JSON` maps workspace IDs to named, fixed checks:

```json
{
  "workspace-a": {
    "release-7": {
      "url": "https://status.example.org/releases/release-7",
      "page_marker": "#release-id",
      "page_text": "Release 7",
      "selector": "#release-status",
      "states": {"ready": "Complete", "pending": "Pending"},
      "success_state": "ready",
      "no_authoritative_api": true
    }
  }
}
```

The example is documentation, not an enabled destination. Review that each URL is a safe read-only public HTTPS document for this workspace. URL credentials, query strings, fragments, nonstandard ports, private addresses, wildcard resources and caller-selected paths are prohibited. `resources` optionally lists up to 24 exact static/XHR resource URLs required by the page. The entire observation is GET-only; pages requiring write requests cannot be verified. Do not list action endpoints even if they use GET.

`no_authoritative_api` is a mandatory operator coverage assertion, not an executor assertion or model conclusion. Set `authoritative_provider` when an API is available; this disables browser verification even when that provider is not connected or installed. The adapter also refuses known Gmail/GitHub API and UI domains, including their subdomains and resources. The operator must review API coverage for other systems; DoneProof cannot determine the existence of every private API from a web page. `enabled: false` disables a check. New API coverage or any configuration change produces a different revision; old selectors and pending publication become UNKNOWN.

The page marker must identify the exact resource. The state element and marker must each have one exact DOM ID, be visible in the initial viewport, unobscured and non-editable. States are a finite list of distinct approved text labels. Only `matched eq true` is executable. A recognized negative label produces FAIL. Missing, duplicate, hidden, unfamiliar, unstable or inappropriate UI produces UNKNOWN. This deliberately excludes scrolling, discovery, arbitrary CSS/JavaScript instructions, iframe outcomes and image-only inference. Approved labels should be nonsensitive; no raw page text is included in observations or receipts.

## Compile and verify

Authenticated `GET /v1/browser/checks` returns only the current tenant's check IDs, revisions and policy status. It discloses no URLs, expected page text, screenshots or credentials. A check is usable only when the provider is installed, the workspace has an enabled approved check, and encryption and the browser runtime are configured. Runtime failures still return UNKNOWN.

Use the returned revision verbatim:

```json
{
  "provider": "browser",
  "id": "p1",
  "description": "Release 7 UI status matches the approved check",
  "selector": {"check_id": "release-7", "revision": "<64-character revision from the catalog>"},
  "predicate": {"op": "eq", "path": "matched", "expected": true}
}
```

Alternatively compile `Verify browser check "release-7" at revision "<revision>" matches` through `/v2/contracts/compile`. `Change` instead of `Verify` requires registration and a false-to-true baseline. The compiler returns a specific lower-assurance quality warning. Preflight performs a separate observation and never supplies the later verification evidence.

Existing `/v1/verify`, registered runs, durable jobs, receipt history and re-verification work with this adapter. Use durable jobs on browser-capable workers for UI workloads. API and workers must share the same check configuration, key ring and installed provider versions. Drain older workers before rollout; do not mix browser-capable and incapable workers on one queue. A browser-capable API process is needed for synchronous browser verification and compilation preflight. The lightweight Vercel/API image intentionally does not bundle Chromium; it continues to serve the existing API providers and returns UNKNOWN for browser execution without the runtime.

## Isolation and budgets

Each observation launches a new sandboxed Chromium process and creates a nonpersistent context. Chromium receives a minimal environment without application credentials. Storage, caches, service workers, downloads, popups, workers, dialogs and WebSockets cannot supply evidence. Browser navigation and subresources are intercepted. Only exact approved HTTPS GET destinations are fetched by the verifier's separate transport; browser headers, cookies and authorization are not forwarded. DNS results must all be public, and the TLS connection is pinned to a checked IP with the original hostname verified. Redirects and unapproved content types fail closed. Chromium DNS is disabled as an additional boundary; use a dedicated constrained worker/container for untrusted page execution.

Limits per observation: 12 seconds internally (also bounded by the engine/job deadline), two concurrent network reads, 32 requests, 1 MiB per response, 4 MiB total, a 1024×768 viewport, three stable state samples, one status-element PNG at most 800×240 pixels and 64 KiB. A bounded mutation flag tracks changes to the evidence elements and their ancestors throughout sampling and screenshot capture, including changes that revert between samples. Requests have five-second overall transport deadlines. The adapter concurrency is two and preflight concurrency one; it does not retry ambiguous or blocked UI. Cancellation closes the context and process. No browser page content, browser errors or network response bodies are logged.

## Receipts and screenshots

Receipts involving browser observations use schema **1.2**. Each condition's `evidence.provenance` identifies `browser_ui`, `lower_than_authoritative_api`, `doneproof.chromium.v1`, a verifier-generated session ID, the check revision, a fixed outcome code, recognized state, sample count and screenshot reference. `executor_supplied` is always false; `fresh_context` is true when collection established a fresh context, otherwise null. The fixed HTTPS source URL is at most 512 characters and contains no query, fragment or user information. Provenance and screenshot SHA-256 are covered by the receipt signature and immutable re-verification chain. Existing 1.0 and 1.1 API receipt canonical bytes remain unchanged. Console rows and printable certificates explicitly label browser evidence as lower assurance.

Migration 6 adds `browser_artifacts` and extends the existing linked-publication guard to schema 1.2. It never rewrites receipts, credentials, contracts or baselines. Screenshots are encrypted using the existing AES-GCM key ring with tenant, provider, artifact and purpose binding. Raw pixels are never returned by an HTTP endpoint or embedded in a receipt. Keep old decryption keys through the retention period when rotating keys.

Screenshots expire after seven days and are bounded to the newest 512 per tenant; busy workspaces may evict them earlier. Receipt hashes remain permanently verifiable after pixel retention ends. Writes purge expired artifacts; run the local operator command during inactive periods as part of normal retention operations:

```sh
python scripts/browser_artifacts.py purge-expired
python scripts/browser_artifacts.py export --tenant workspace-a --artifact bs_... --output review.png
```

Export requires direct database/key access, refuses to overwrite a file and creates an audit event. Treat exported images as restricted workspace data. Browser screenshots do not become evidence for another verification.

## Runtime and validation

```sh
pip install -e '.[dev,browser]'
python -m playwright install --with-deps --only-shell chromium
DONEPROOF_BROWSER_TESTS=1 pytest tests/test_browser_chromium.py -q
docker build -f Dockerfile.browser -t doneproof:browser .
```

The separate image keeps the normal API/Docker and Vercel dependency footprint unchanged. Run it as its non-root user with `--init`, memory/PID limits and a Chromium-compatible seccomp profile; never disable the Chromium sandbox. Host support for unprivileged user namespaces is required. CI validates the browser image offline with network disabled:

```sh
docker run --rm --init --network=none --shm-size=256m --memory=1g --pids-limit=256 \
  --security-opt seccomp=deploy/browser-seccomp.json \
  -v "$PWD/scripts/browser_smoke.py:/app/browser_smoke.py:ro" \
  doneproof:browser python browser_smoke.py
```

`deploy/browser-seccomp.json` is the Apache-2.0 Playwright v1.62.0 [Docker profile](https://github.com/microsoft/playwright/blob/v1.62.0/utils/docker/seccomp_profile.json), SHA-256 `cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849`. See the [Playwright container guidance](https://playwright.dev/python/docs/docker), [nonpersistent contexts](https://playwright.dev/python/docs/api/class-browsercontext) and [HTTPX TLS hostname/IP pinning](https://www.python-httpx.org/advanced/extensions/#sni_hostname). The profile enables user-namespace creation while retaining syscall filtering; it is not an unconfined profile.

Tests use offline pages, never design-partner accounts: genuine Chromium success/negative states, fresh storage, login/challenge/interstitials, unstable/missing/duplicate UI, guidance rejection, request blocking and cancellation; transport policy tests use local mocks; tenant/persistence/migration/key-rotation tests run against SQLite and PostgreSQL. The production smoke script checks authentication on the browser catalog without accessing customer pages.
