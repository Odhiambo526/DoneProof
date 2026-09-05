import { readFileSync } from 'node:fs';
import { createHmac } from 'node:crypto';
import test from 'node:test';
import assert from 'node:assert/strict';
import { DoneProof, DoneProofError, DoneProofTimeout, VerificationCancelled, CompatibilityError,
  DuplicateEvent, CallbackError, MemoryReplayStore, parseModel, verifyCallback, verifyReceipt } from '../dist/index.js';

const fixtures = JSON.parse(readFileSync(new URL('./fixtures.json', import.meta.url)));
const ready = fixtures.ready_session;
const response = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), { status, headers });

for (const fixture of fixtures.receipts) test('Pinned exact receipt schema ' + fixture.receipt.schema_version, () => {
  assert(verifyReceipt(fixture.receipt, fixture.signed_payload_b64, fixture.pinned_public_key));
  assert(!verifyReceipt({ ...fixture.receipt, verdict: 'VERIFIED' }, fixture.signed_payload_b64, fixture.pinned_public_key));
  assert(!verifyReceipt(fixture.receipt, fixture.signed_payload_b64, Buffer.alloc(32).toString('base64')));
  assert(!verifyReceipt({ ...fixture.receipt, schema_version: '0.9' }, fixture.signed_payload_b64, fixture.pinned_public_key));
  assert(!verifyReceipt(fixture.receipt, Buffer.from('{}').toString('base64'), fixture.pinned_public_key));
});

test('Receipt downgrade and browser assurance fail closed', () => {
  const browser = fixtures.receipts[2].receipt;
  assert.throws(() => parseModel('VerificationReceipt', { ...browser, schema_version: '1.0' }), CompatibilityError);
  assert.throws(() => parseModel('AssuranceSession', { ...ready, protocol_version: '2.0' }), CompatibilityError);
  assert.throws(() => parseModel('AssuranceSession', { ...ready, compiler: null }), CompatibilityError);
  assert.throws(() => parseModel('AssuranceSession', { ...ready, state: 'NEEDS_CLARIFICATION' }), CompatibilityError);
  assert.throws(() => parseModel('AssuranceSession', { ...ready, evidence: [{ condition: 'p1', provider: 'browser',
    evidence_class: 'provider_observation', assurance_level: 'provider_declared' }] }), CompatibilityError);
  assert.throws(() => parseModel('VerificationReceipt', { ...browser, duration_ms: 9007199254740993 }), CompatibilityError);
});

for (const status of [429, 500, 503, 'lost']) test('Retry identity after ' + status, async () => {
  const seen = [];
  const dp = new DoneProof({ apiKey: 'secret-sentinel', fetch: async (url, options) => {
    seen.push({ url, options });
    if (seen.length === 1) { if (status === 'lost') throw Error('secret-sentinel'); return response({}, status); }
    return response(ready);
  } });
  const session = await dp.assurance.prepare({ task: 'Close issue #12 in acme/api', idempotencyKey: 'business-1' });
  assert.equal(session.id, ready.id); assert.equal(seen.length, 2);
  assert.equal(seen[0].options.body, seen[1].options.body);
  assert.equal(seen[0].options.headers['Idempotency-Key'], seen[1].options.headers['Idempotency-Key']);
  assert.equal(seen[0].options.headers['X-Request-ID'], seen[1].options.headers['X-Request-ID']);
});

for (const status of [400, 401, 403, 409, 422]) test('Semantic HTTP error ' + status + ' is not retried or exposed', async () => {
  const logs = []; let count = 0;
  const dp = new DoneProof({ apiKey: 'secret-sentinel', logHook: e => logs.push(e), fetch: async () => { count++; return response({ secret: 'secret-sentinel' }, status); } });
  await assert.rejects(dp.assurance.prepare({ task: 'Close issue #12 in acme/api', idempotencyKey: 'business-1' }), e => {
    assert(e instanceof DoneProofError); assert(!JSON.stringify(e).includes('sentinel')); return true;
  });
  assert.equal(count, 1); assert(!JSON.stringify(logs).includes('sentinel'));
});

test('Retry-After respects absolute deadline', async () => {
  let calls = 0;
  const dp = new DoneProof({ apiKey: 'k', fetch: async () => { calls++; return response({}, 429, { 'Retry-After': '100' }); } });
  await assert.rejects(dp.assurance.prepare({ task: 'A valid task', idempotencyKey: 'key', timeout: 0.1 }), DoneProofTimeout);
  assert.equal(calls, 1);
});

test('Local cancellation sends no request', async () => {
  const dp = new DoneProof({ apiKey: 'k', fetch: () => { throw new Error('unexpected request'); } });
  await assert.rejects(dp.assurance.verify(ready.id, { signal: AbortSignal.abort() }), VerificationCancelled);
});

test('Cached provider declarations are bounded copies; capabilities stay live', async () => {
  let calls = 0;
  const dp = new DoneProof({ apiKey: 'k', fetch: async url => { calls++; return response(url.endsWith('/providers') ? { sdk_version: 1, providers: [] }
    : { version: '0.9.4', environment: 'test', compiler: 'available', signing_key_id: 'key', providers: [] }); } });
  const first = await dp.providers.declarations(); first.providers.push({ provider_id: 'injected' });
  assert.equal((await dp.providers.declarations()).providers.length, 0);
  await dp.providers.available(); await dp.providers.available();
  assert.equal(calls, 3);
});

test('Signed callback replay, tampering and event-header substitution', async () => {
  const now = Math.floor(Date.now() / 1000), secret = 's'.repeat(32);
  const event = { event_id: 've_' + 'a'.repeat(32), job_id: 'vj_' + 'a'.repeat(32),
    receipt_id: 'vr_' + 'a'.repeat(32), state: 'COMPLETE', finished_at: now };
  const body = Buffer.from(JSON.stringify(event));
  const signature = createHmac('sha256', secret).update(now + '.').update(body).digest('hex');
  const headers = { 'X-DoneProof-Event': event.event_id, 'X-DoneProof-Timestamp': String(now), 'X-DoneProof-Signature': 'sha256=' + signature };
  const replayStore = new MemoryReplayStore();
  assert.deepEqual(await verifyCallback(body, headers, { secret, replayStore }), event);
  await assert.rejects(verifyCallback(body, headers, { secret, replayStore }), DuplicateEvent);
  await assert.rejects(verifyCallback(Buffer.concat([body, Buffer.from(' ')]), headers, { secret, replayStore }), CallbackError);
  await assert.rejects(verifyCallback(body, { ...headers, 'X-DoneProof-Event': 'other' }, { secret, replayStore }), CallbackError);
  await assert.rejects(verifyCallback(body, headers, { secret, replayStore, now: now + 301 }), CallbackError);
});

test('No credential-bearing base URL or cross-origin custom transport path', async () => {
  for (const baseUrl of ['https://key@example.org', 'https://example.org/?token=abc', 'http://remote.example.org'])
    assert.throws(() => new DoneProof({ apiKey: 'k', baseUrl }), DoneProofError);
  const dp = new DoneProof({ apiKey: 'k' });
  await assert.rejects(dp.request('GET', '//evil.example.org', 'CapabilityResponse'), DoneProofError);
});
