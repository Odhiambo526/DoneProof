import { createHash, createHmac, createPublicKey, randomUUID, timingSafeEqual, verify } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import { Ajv2020 } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import type { ValidateFunction } from 'ajv';
import { schemas } from './schemas.js';
import type { AssuranceSession, CapabilityResponse, CompilationResult, CompletionEvent, ConnectionList,
  ConnectionOnboarding, ConnectionView, DurableJob, ModelMap, ProviderCatalog, VerificationReceipt } from './models.js';
export type * from './models.js';

export const VERSION = '0.9.4';
const terminal = new Set(['COMPLETE', 'PARTIAL_FAILURE', 'EXPIRED', 'INTERNAL_ERROR']);
const ajv = new Ajv2020({ strict: false, allErrors: false, validateFormats: false });
// Formats are registered explicitly; schemas remain local and cannot load URLs.
const formatPlugin = addFormats as unknown as (instance: Ajv2020) => void;
formatPlugin(ajv);
const validators = new Map<keyof ModelMap, ValidateFunction>();

export class DoneProofError extends Error {
  constructor(readonly code: string, readonly status?: number, readonly requestId?: string) {
    super(code); this.name = new.target.name;
  }
}
export class AuthenticationError extends DoneProofError {}
export class ConflictError extends DoneProofError {}
export class RateLimitError extends DoneProofError {}
export class DoneProofTimeout extends DoneProofError {}
export class VerificationCancelled extends DoneProofError {}
export class CompatibilityError extends DoneProofError {}
export class CallbackError extends DoneProofError {}
export class DuplicateEvent extends CallbackError {}

export function parseModel<K extends keyof ModelMap>(name: K, value: unknown): ModelMap[K] {
  let validator = validators.get(name);
  if (!validator) { validator = ajv.compile(schemas[name]); validators.set(name, validator); }
  if (!validator(value)) throw new CompatibilityError('unsupported_or_invalid_response');
  if (name === 'AssuranceSession') {
    const session = value as AssuranceSession;
    if (session.protocol_version !== '1.0') throw new CompatibilityError('unsupported_protocol_version');
    if (session.state === 'READY_FOR_EXECUTION' && (!session.contract || !session.trusted_task_started_at
        || session.compiler?.status !== 'valid_contract' || session.compiler.clarification_requirements?.length))
      throw new CompatibilityError('invalid_ready_session');
    if (session.state === 'NEEDS_CLARIFICATION' && (session.contract || !session.compiler?.clarification_requirements?.length
        || session.compiler.status === 'valid_contract')) throw new CompatibilityError('invalid_clarification');
    if (session.verdict && session.verdict !== session.receipt?.verdict) throw new CompatibilityError('unbound_verdict');
    if (session.receipt) validateReceipt(session.receipt);
    for (const evidence of session.evidence ?? []) {
      if ((evidence.provider === 'browser' || evidence.provenance) && (evidence.evidence_class !== 'browser_ui'
          || evidence.assurance_level !== 'lower_than_authoritative_api' || !evidence.provenance))
        throw new CompatibilityError('browser_assurance_mismatch');
    }
  }
  if (name === 'VerificationReceipt') validateReceipt(value as VerificationReceipt);
  return value as ModelMap[K];
}

function validateReceipt(receipt: VerificationReceipt): void {
  if (!['1.0', '1.1', '1.2'].includes(receipt.schema_version ?? '')) throw new CompatibilityError('unsupported_receipt_schema');
  const browser = receipt.results.some(r => r.evidence.provider === 'browser' || r.evidence.provenance);
  if (browser && (receipt.schema_version !== '1.2' || receipt.results.some(r => r.evidence.provider === 'browser' && !r.evidence.provenance)))
    throw new CompatibilityError('browser_receipt_downgrade');
  if (receipt.schema_version === '1.0' && (receipt.recovery || receipt.previous_receipt_id || receipt.previous_receipt_hash || receipt.remediation?.length))
    throw new CompatibilityError('receipt_schema_downgrade');
  if (receipt.schema_version !== '1.0' && (!receipt.recovery
      || Boolean(receipt.previous_receipt_id) !== Boolean(receipt.previous_receipt_hash)
      || Boolean(receipt.previous_receipt_id) !== ((receipt.recovery.attempt ?? 0) > 0)
      || !receipt.previous_receipt_id && receipt.recovery.chain_id !== receipt.receipt_id))
    throw new CompatibilityError('invalid_receipt_lineage');
}

function identifier(value: string): string {
  if (!/^[A-Za-z0-9_-]{1,200}$/.test(value)) throw new DoneProofError('invalid_resource_identifier');
  return value;
}
function mutationKey(value: string): string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9._:-]{1,200}$/.test(value)) throw new DoneProofError('stable_idempotency_key_required');
  return value;
}
function deadline(seconds: number): number {
  if (!Number.isFinite(seconds) || seconds <= 0) throw new DoneProofError('invalid_timeout');
  return performance.now() + seconds * 1000;
}
function check(end: number, signal?: AbortSignal): void {
  if (signal?.aborted) throw new VerificationCancelled('local_wait_cancelled');
  if (performance.now() >= end) throw new DoneProofTimeout('request_deadline_exceeded');
}
async function pause(seconds: number, end: number, signal?: AbortSignal): Promise<void> {
  check(end, signal);
  await new Promise<void>((resolve, reject) => {
    const abort = (): void => { clearTimeout(timer); reject(new VerificationCancelled('local_wait_cancelled')); };
    const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve(); },
      Math.max(0, Math.min(seconds * 1000, end - performance.now())));
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
  });
  check(end, signal);
}
function retryAfter(headers: Headers): number {
  const raw = headers.get('Retry-After');
  if (!raw) return 0;
  const numeric = Number(raw);
  const value = Number.isNaN(numeric) ? (Date.parse(raw) - Date.now()) / 1000 : numeric;
  return Number.isFinite(value) ? Math.max(0, value) : 0;
}

export interface RequestLog { method: 'GET' | 'POST'; status: number | undefined; attempt: number; requestId: string; durationMs: number }
export interface ClientOptions { apiKey: string; baseUrl?: string; timeout?: number; fetch?: typeof fetch; logHook?: (event: RequestLog) => void }
export interface WaitOptions { timeout?: number; signal?: AbortSignal }
export interface VerifyOptions extends WaitOptions { wait?: boolean; deadlineSeconds?: number; callbackId?: string }
export interface ReverifyOptions extends VerifyOptions { previousReceiptId: string; idempotencyKey: string }
export interface PrepareOptions extends WaitOptions { task: string; context?: Record<string, unknown>; idempotencyKey: string }
interface RequestOptions { body?: unknown; key?: string; end?: number; signal?: AbortSignal | undefined }

export class DoneProof {
  readonly assurance: Assurance;
  readonly providers: Providers;
  readonly baseUrl: string;
  readonly #apiKey: string;
  private readonly timeout: number;
  private readonly fetcher: typeof fetch;
  private readonly logHook: ((event: RequestLog) => void) | undefined;
  constructor(options: ClientOptions) {
    let url: URL;
    try { url = new URL(options.baseUrl ?? 'https://www.getdoneproof.com'); } catch { throw new DoneProofError('invalid_base_url'); }
    if (url.username || url.password || url.search || url.hash || url.pathname !== '/' ||
        (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))))
      throw new DoneProofError('invalid_base_url');
    this.baseUrl = url.origin; this.#apiKey = options.apiKey; this.timeout = options.timeout ?? 30;
    deadline(this.timeout);
    this.fetcher = options.fetch ?? fetch; this.logHook = options.logHook;
    this.assurance = new Assurance(this); this.providers = new Providers(this);
  }

  async request<K extends keyof ModelMap>(method: 'GET' | 'POST', path: string, model: K, options: RequestOptions = {}): Promise<ModelMap[K]> {
    // This transport is public for custom thin adapters, but never accepts another origin.
    if (!/^\/(?:v1|v2)\/[A-Za-z0-9_/?=.&:-]+$/.test(path) || path.includes('..')) throw new DoneProofError('invalid_api_path');
    const end = options.end ?? deadline(this.timeout), requestId = 'req_' + randomUUID().replaceAll('-', '');
    const headers: Record<string, string> = { 'X-DoneProof-Key': this.#apiKey, 'Accept': 'application/json',
      'Content-Type': 'application/json', 'User-Agent': 'doneproof-typescript/' + VERSION, 'X-Request-ID': requestId };
    if (options.key) headers['Idempotency-Key'] = mutationKey(options.key);
    let body: string | undefined;
    try { body = options.body === undefined ? undefined : JSON.stringify(options.body); } catch { throw new DoneProofError('invalid_client_input'); }
    for (let attempt = 0; attempt < 4; attempt++) {
      check(end, options.signal);
      let status: number | undefined, after = 0;
      const started = performance.now();
      const budget = AbortSignal.timeout(Math.max(1, Math.ceil(Math.min(end - started, this.timeout * 1000))));
      const signal = options.signal ? AbortSignal.any([options.signal, budget]) : budget;
      try {
        const response = await this.fetcher(this.baseUrl + path, { method, headers, ...(body === undefined ? {} : { body }), signal, redirect: 'manual' });
        status = response.status; after = retryAfter(response.headers);
        if (response.ok) {
          const reader = response.body?.getReader(), chunks: Uint8Array[] = [];
          let size = 0;
          try {
            if (reader) while (true) {
              const chunk = await reader.read(); if (chunk.done) break;
              check(end, options.signal); size += chunk.value.byteLength;
              if (size > 16 * 1024 * 1024) throw new CompatibilityError('response_too_large');
              chunks.push(chunk.value);
            }
          } finally { await reader?.cancel(); }
          let value: unknown;
          try { value = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { throw new CompatibilityError('invalid_json_response'); }
          return parseModel(model, value);
        }
        await response.body?.cancel();
      } catch (error) {
        if (error instanceof DoneProofError) throw error;
        check(end, options.signal);
        status = undefined;
      } finally {
        this.logHook?.({ method, status, attempt: attempt + 1, requestId, durationMs: performance.now() - started });
      }
      const transient = status === undefined || status === 429 || status >= 500 && status <= 599;
      if (!transient || method === 'POST' && !options.key || attempt === 3) {
        const ErrorType = status === 401 || status === 403 ? AuthenticationError : status === 409 ? ConflictError : status === 429 ? RateLimitError : DoneProofError;
        throw new ErrorType(status === undefined ? 'network_unavailable' : 'request_rejected', status, requestId);
      }
      const seconds = Math.max(after, Math.min(4, 0.25 * 2 ** attempt) * (0.5 + Math.random() / 2));
      if (seconds * 1000 >= end - performance.now()) throw new DoneProofTimeout('retry_exceeds_deadline', status, requestId);
      await pause(seconds, end, options.signal);
    }
    throw new DoneProofError('request_unavailable');
  }

  capabilities(): Promise<CapabilityResponse> { return this.request('GET', '/v1/capabilities', 'CapabilityResponse'); }
  getJob(jobId: string): Promise<DurableJob> { return this.request('GET', '/v1/jobs/' + identifier(jobId), 'DurableJob'); }
  cancelJob(jobId: string): Promise<DurableJob> {
    return this.request('POST', '/v1/jobs/' + identifier(jobId) + '/cancel', 'DurableJob', { key: 'cancel:' + jobId });
  }
  waitForVerification(jobId: string, options: WaitOptions = {}): Promise<DurableJob> {
    return this.waitUntil(jobId, deadline(options.timeout ?? 120), options.signal);
  }
  async waitUntil(jobId: string, end: number, signal?: AbortSignal): Promise<DurableJob> {
    let delay = 0.25;
    while (true) {
      const job = await this.request('GET', '/v1/jobs/' + identifier(jobId), 'DurableJob', { end, signal });
      if (terminal.has(job.state)) return job;
      await pause(delay * (0.5 + Math.random() / 2), end, signal); delay = Math.min(5, delay * 2);
    }
  }
  listConnections(): Promise<ConnectionList> { return this.request('GET', '/v1/connections', 'ConnectionList'); }
  connectionStatus(id: string): Promise<ConnectionView> { return this.request('GET', '/v1/connections/' + identifier(id), 'ConnectionView'); }
  async beginConnection(provider: string): Promise<ConnectionOnboarding> {
    const listing = await this.listConnections();
    if (!listing.providers.some(p => p.provider === provider && p.onboarding_available)) throw new DoneProofError('connection_onboarding_unavailable');
    return { authorization_url: this.baseUrl + '/connections#' + identifier(provider), mode: 'trusted_browser_settings', administrator_login_required: true };
  }
  disconnect(id: string, options: { idempotencyKey: string; expectedRevision: number }): Promise<ConnectionView> {
    if (!Number.isSafeInteger(options.expectedRevision) || options.expectedRevision < 0) throw new DoneProofError('invalid_connection_revision');
    return this.request('POST', '/v2/connections/' + identifier(id) + '/disconnect', 'ConnectionView',
      { body: { expected_revision: options.expectedRevision }, key: mutationKey(options.idempotencyKey) });
  }
}

export class Assurance {
  constructor(private readonly client: DoneProof) {}
  prepare(options: PrepareOptions): Promise<AssuranceSession> {
    const body = { task: options.task, context: options.context ?? {} }; parseModel('PrepareSession', body);
    return this.client.request('POST', '/v1/assurance/sessions', 'AssuranceSession', { body, key: mutationKey(options.idempotencyKey),
      end: deadline(options.timeout ?? 150), signal: options.signal });
  }
  get(id: string): Promise<AssuranceSession> { return this.client.request('GET', '/v1/assurance/sessions/' + identifier(id), 'AssuranceSession'); }
  async verify(id: string, options: VerifyOptions = {}): Promise<AssuranceSession> {
    const end = deadline(options.timeout ?? 120), body = { deadline_seconds: options.deadlineSeconds ?? 300, callback_id: options.callbackId ?? null };
    parseModel('VerifySession', body);
    const key = 'verify:' + createHash('sha256').update(JSON.stringify([id, body])).digest('hex');
    const result = await this.client.request('POST', '/v1/assurance/sessions/' + identifier(id) + '/verify', 'AssuranceSession', { body, key, end, signal: options.signal });
    return this.finish(result, options.wait ?? false, end, options.signal);
  }
  async reverify(id: string, options: ReverifyOptions): Promise<AssuranceSession> {
    const end = deadline(options.timeout ?? 120), body = { previous_receipt_id: options.previousReceiptId,
      deadline_seconds: options.deadlineSeconds ?? 300, callback_id: options.callbackId ?? null };
    parseModel('ReverifySession', body);
    const result = await this.client.request('POST', '/v1/assurance/sessions/' + identifier(id) + '/reverify', 'AssuranceSession',
      { body, key: mutationKey(options.idempotencyKey), end, signal: options.signal });
    return this.finish(result, options.wait ?? false, end, options.signal);
  }
  private async finish(session: AssuranceSession, wait: boolean, end: number, signal?: AbortSignal): Promise<AssuranceSession> {
    if (wait && session.current_job_id) {
      await this.client.waitUntil(session.current_job_id, end, signal);
      return this.client.request('GET', '/v1/assurance/sessions/' + identifier(session.id), 'AssuranceSession', { end, signal });
    }
    return session;
  }
  async waitForVerification(id: string, options: WaitOptions = {}): Promise<AssuranceSession> {
    const end = deadline(options.timeout ?? 120);
    const session = await this.client.request('GET', '/v1/assurance/sessions/' + identifier(id), 'AssuranceSession', { end, signal: options.signal });
    if (!session.current_job_id) throw new ConflictError('session_has_no_verification_job');
    return this.finish(session, true, end, options.signal);
  }
}

export class Providers {
  private catalog: ProviderCatalog | undefined;
  private until = 0;
  constructor(private readonly client: DoneProof) {}
  available(): Promise<CapabilityResponse> { return this.client.capabilities(); }
  async declarations(options: { refresh?: boolean } = {}): Promise<ProviderCatalog> {
    if (!this.catalog || options.refresh || performance.now() >= this.until) {
      this.catalog = await this.client.request('GET', '/v1/providers', 'ProviderCatalog'); this.until = performance.now() + 60000;
    }
    return structuredClone(this.catalog);
  }
  forTask(options: { task: string; context?: Record<string, unknown> }): Promise<CompilationResult> {
    return this.client.request('POST', '/v2/contracts/compile', 'CompilationResult', { body: { task: options.task, context: options.context ?? {} } });
  }
}

export interface ReplayStore { claim(eventId: string, expiresAt: number): boolean | Promise<boolean> }
export class MemoryReplayStore implements ReplayStore {
  private readonly seen = new Map<string, number>();
  constructor(private readonly capacity = 10000) {}
  claim(eventId: string, expiresAt: number): boolean {
    for (const [id, expiry] of this.seen) if (expiry <= Date.now() / 1000) this.seen.delete(id);
    if (this.seen.has(eventId)) return false;
    if (this.seen.size >= this.capacity) throw new CallbackError('deduplication_capacity_exceeded');
    this.seen.set(eventId, expiresAt); return true;
  }
}
export async function verifyCallback(body: Uint8Array, headers: Headers | Record<string, string>, options: {
  secret: string; replayStore: ReplayStore; maxSkewSeconds?: number; now?: number
}): Promise<CompletionEvent> {
  const fields = new Headers(headers), timestamp = fields.get('X-DoneProof-Timestamp') ?? '', signature = fields.get('X-DoneProof-Signature') ?? '';
  const now = options.now ?? Date.now() / 1000, skew = options.maxSkewSeconds ?? 300;
  if (body.byteLength > 65536 || options.secret.length < 32 || skew < 1 || skew > 600 || !Number.isFinite(now) || !Number.isFinite(skew)
      || !/^[0-9]{1,12}$/.test(timestamp) || Math.abs(now - Number(timestamp)) > skew || !/^sha256=[a-f0-9]{64}$/.test(signature))
    throw new CallbackError('invalid_callback_authentication');
  const expected = createHmac('sha256', options.secret).update(timestamp + '.').update(body).digest();
  if (!timingSafeEqual(expected, Buffer.from(signature.slice(7), 'hex'))) throw new CallbackError('invalid_callback_authentication');
  let event: CompletionEvent;
  try { event = parseModel('CompletionEvent', JSON.parse(Buffer.from(body).toString('utf8'))); } catch { throw new CallbackError('invalid_callback_payload'); }
  const receipt = ['COMPLETE', 'PARTIAL_FAILURE'].includes(event.state) ? 'vr_' + event.job_id.slice(3) : null;
  if (event.event_id !== fields.get('X-DoneProof-Event') || event.event_id.slice(3) !== event.job_id.slice(3)
      || event.receipt_id !== receipt || event.finished_at > now + skew) throw new CallbackError('invalid_callback_payload');
  let claimed: boolean;
  try { claimed = await options.replayStore.claim(event.event_id, now + 86400 + skew); }
  catch { throw new CallbackError('receiver_deduplication_unavailable'); }
  if (!claimed) throw new DuplicateEvent('callback_already_received');
  return event;
}

export function verifyReceipt(receipt: VerificationReceipt, signedPayloadB64: string, pinnedPublicKey: string): boolean {
  // Verify exact signed bytes; never reconstruct Python floating-point JSON in JS.
  try {
    validateReceipt(receipt);
    const key = Buffer.from(pinnedPublicKey, 'base64'), embedded = Buffer.from(receipt.public_key ?? '', 'base64');
    if (key.length !== 32 || embedded.length !== 32 || !timingSafeEqual(key, embedded) || receipt.signature_alg !== 'Ed25519') return false;
    const bytes = Buffer.from(signedPayloadB64, 'base64');
    if (bytes.length > 16 * 1024 * 1024 || createHash('sha256').update(key).digest('hex').slice(0, 16) !== receipt.key_id
        || createHash('sha256').update(bytes).digest('hex') !== receipt.receipt_hash) return false;
    const publicKey = createPublicKey({ key: Buffer.concat([Buffer.from('302a300506032b6570032100', 'hex'), key]), format: 'der', type: 'spki' });
    if (!verify(null, bytes, publicKey, Buffer.from(receipt.signature ?? '', 'base64'))) return false;
    const { signature: _signature, receipt_hash: _hash, ...payload } = receipt;
    return isDeepStrictEqual(JSON.parse(bytes.toString('utf8')), payload);
  } catch { return false; }
}
