import { readFileSync, writeFileSync } from 'node:fs';
import { DoneProof, parseModel } from '../dist/index.js';
const fixture = JSON.parse(readFileSync(new URL('./fixtures.json', import.meta.url)));
const value = fixture.ready_session, body = JSON.stringify(value);
parseModel('AssuranceSession', value); // Separate cold schema compilation from steady-state costs.
const serialization = performance.now();
for (let i = 0; i < 1000; i++) JSON.stringify(parseModel('AssuranceSession', JSON.parse(body)));
const serializationMs = (performance.now() - serialization) / 1000;
const dp = new DoneProof({ apiKey: 'fixture-key', fetch: async () => new Response(body) });
const start = performance.now();
for (let i = 0; i < 100; i++) await dp.assurance.get(value.id);
const result = { fixture: true, network: 'mock fetch, no network', typescript_serialize_parse_ms_mean: serializationMs,
  typescript_sdk_no_network_ms_mean: (performance.now() - start) / 100, session_bytes: Buffer.byteLength(body) };
const output = process.argv[2];
if (output) writeFileSync(output, JSON.stringify(result, null, 2) + '\n');
console.log(JSON.stringify(result));
