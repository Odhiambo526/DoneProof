import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { DoneProof, verifyReceipt } from '../src/index.js';

console.log('OFFLINE DEMO FIXTURE: local issue state and test key; not real GitHub evidence.');
// This file runs from example-dist/examples after the strict example build.
const runtime = fileURLToPath(new URL('../../../../examples/fixture_runtime.py', import.meta.url));
const child = spawn(process.env.PYTHON ?? 'python', [runtime], { stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true });
assert(child.stdin && child.stdout);
const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
try {
  const line = await lines.next();
  assert(!line.done);
  const config = JSON.parse(line.value) as { baseUrl: string; pinnedPublicKey: string; fixture: boolean };
  assert.equal(config.fixture, true);
  const dp = new DoneProof({ apiKey: 'fixture-key', baseUrl: config.baseUrl });
  const session = await dp.assurance.prepare({ task: 'Close issue #12 in acme/api', idempotencyKey: 'invoice-1842' });
  assert.equal(session.state, 'READY_FOR_EXECUTION');
  console.log('Prepared. External fixture agent claims success; issue is still open.');
  const result = await dp.assurance.verify(session.id, { wait: true });
  assert.equal(result.verdict, 'FAILED');
  console.log('Independent result:', result.verdict);
  for (const issue of result.remediation ?? []) console.log(issue.action_hint);
  child.stdin.write('repair\n');
  const repairLine = await lines.next();
  assert(!repairLine.done);
  assert.equal((JSON.parse(repairLine.value) as { fixture_repaired: boolean }).fixture_repaired, true);
  assert(result.receipt?.receipt_id);
  const repaired = await dp.assurance.reverify(session.id, { previousReceiptId: result.receipt.receipt_id,
    idempotencyKey: 'invoice-1842-repair-1', wait: true });
  assert.equal(repaired.verdict, 'VERIFIED');
  assert.equal(repaired.lineage?.length, 2);
  assert(repaired.receipt && repaired.signed_payload_b64);
  assert(verifyReceipt(repaired.receipt, repaired.signed_payload_b64, config.pinnedPublicKey));
  console.log('VERIFIED; immutable two-receipt chain; pinned Ed25519 verification passed.');
} finally {
  child.stdin.end('stop\n');
  const timeout = setTimeout(() => child.kill(), 5000);
  await new Promise<void>(resolve => { if (child.exitCode !== null) resolve(); else child.once('exit', () => resolve()); });
  clearTimeout(timeout);
}
