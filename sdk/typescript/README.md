# DoneProof TypeScript SDK

Node 22+, ESM, strict TypeScript, runtime schema validation, no frontend or agent
framework dependency. Build with `npm ci && npm run build`; validate with
`npm run lint && npm run typecheck && npm test`. Run `npm run demo` for the offline
false-success and repair example (Python DoneProof checkout must be installed).

```ts
import { DoneProof, verifyReceipt } from '@doneproof/sdk';
const dp = new DoneProof({ apiKey: process.env.DONEPROOF_API_KEY! });
const session = await dp.assurance.prepare({
  task: 'Close issue #12 in acme/api', idempotencyKey: 'issue-12-close-v1'
});
// Only execute your external agent after state === 'READY_FOR_EXECUTION'.
// Its outputs are never passed as evidence.
```

See the repository's `docs/assurance-sessions.md` for waiting, cancellation,
callbacks, connections, pinned verification and compatibility requirements.
`docs/agent-integration-recipes.md` covers OpenAI Agents, LangGraph and CrewAI.
The package is prepared for review, not published by this change.
