"""Compose the existing compiler, registered boundary, queue and recovery chain.

No provider action or executor observation input exists in this service. A lost
preparation never recaptures a baseline: its key remains permanently failed.
"""
import asyncio
import base64
import json
from datetime import datetime, timezone
from uuid import uuid4

from .assurance_models import AssuranceSession, DurableJob, EvidenceAssurance, ReceiptLink, assurance_summary
from .compilation_models import CompilationResult, issue
from .domain import ConditionStatus
from .job_models import TERMINAL
from .job_store import IdempotencyConflict, canonical, digest
from .recovery_store import RecoveryError, RecoveryStore
from .signing import ReceiptSigner


def instant(value):
    return datetime.fromtimestamp(value, timezone.utc)


class AssuranceService(RecoveryStore):
    def __init__(self, app):
        super().__init__(app.state.store, app.state.settings.max_reverification_attempts)
        self.app = app

    def session_row(self, con, tenant, identifier, lock=False):
        row = self._row(self.execute(con, 'SELECT * FROM assurance_sessions WHERE tenant_id=? AND id=?' +
                                     (self.lock() if lock else ''), (tenant, identifier)))
        if not row:
            raise RecoveryError('session_not_found', 404)
        return row

    def reserve(self, tenant, key, req):
        with self.transaction() as con:
            now = self.now(con)
            request_data = req.model_dump(mode='json', exclude={'require_transition'} if not req.require_transition else set())
            identifier, request_hash = 'as_' + uuid4().hex, digest(canonical(request_data))
            inserted = self.execute(con, '''INSERT INTO assurance_sessions
                (tenant_id,id,idempotency_hash,request_hash,task,preparation_state,created_at,updated_at,preparation_deadline)
                VALUES(?,?,?,?,?,'PREPARING',?,?,?) ON CONFLICT(tenant_id,idempotency_hash) DO NOTHING''',
                (tenant, identifier, digest(key), request_hash, req.task, now, now, now + 150)).rowcount
            row = self._row(self.execute(con, '''SELECT * FROM assurance_sessions
                WHERE tenant_id=? AND idempotency_hash=?''' + self.lock(), (tenant, digest(key))))
            if row['request_hash'] != request_hash:
                raise IdempotencyConflict
            return row, bool(inserted)

    def fail_preparation(self, tenant, identifier):
        with self.transaction() as con:
            self.execute(con, '''UPDATE assurance_sessions SET preparation_state='PREPARATION_FAILED',updated_at=?
                WHERE tenant_id=? AND id=? AND preparation_state='PREPARING' ''', (self.now(con), tenant, identifier))

    async def prepare(self, tenant, key, req):
        row, created = await asyncio.to_thread(self.reserve, tenant, key, req)
        if created:
            try:
                async with asyncio.timeout(145):
                    result = await self.app.state.compiler.compile(req.task, req.context, tenant)
                    valid = result.status == 'valid_contract'
                    if valid != bool(result.contract) or valid == bool(result.clarification_requirements):
                        raise ValueError('Inconsistent compiler result')
                    contract, baselines = None, []
                    if valid:
                        if not self.registry.accepts(result.contract):
                            raise ValueError('Provider declaration mismatch')
                        contract = result.contract.model_copy(deep=True)
                        if req.require_transition:
                            for condition in contract.postconditions:
                                if not self.registry.require(condition.provider).manifest.transition_support:
                                    raise ValueError('Provider cannot prove transitions')
                                condition.require_change = True
                        contract.id = 'cc_' + uuid4().hex[:16]
                        contract.task_started_at = contract.created_at = datetime.now(timezone.utc)
                        baselines = await self.app.state.engine.snapshot(contract, tenant)
                        expected = {p.id for p in contract.postconditions if p.require_change}
                        if {b.id for b in baselines} != expected or any(b.status == ConditionStatus.UNKNOWN for b in baselines):
                            result = self.app.state.compiler._result([issue('provider_unavailable')])
                            contract, baselines = None, []
                        else:
                            result = result.model_copy(update={'contract': contract})
                    await asyncio.to_thread(self.finish_preparation, tenant, row['id'], result, contract, baselines)
            except asyncio.CancelledError:
                await asyncio.to_thread(self.fail_preparation, tenant, row['id'])
                raise
            except Exception:
                await asyncio.to_thread(self.fail_preparation, tenant, row['id'])
        return await asyncio.to_thread(self.view, tenant, row['id'])

    def finish_preparation(self, tenant, identifier, result, contract, baselines):
        with self.transaction() as con:
            row = self.session_row(con, tenant, identifier, lock=True)
            now = self.now(con)
            if row['preparation_state'] != 'PREPARING' or row['preparation_deadline'] <= now:
                raise RecoveryError('preparation_expired')
            providers = []
            if contract:
                self.execute(con, '''INSERT INTO contracts(tenant_id,id,task,body_json,created_at) VALUES(?,?,?,?,?)''',
                    (tenant, contract.id, contract.task, contract.model_dump_json(), contract.created_at.isoformat()))
                for provider, fingerprint in self.provider_manifest(contract).items():
                    providers.append({'provider': provider, 'fingerprint': fingerprint,
                                      'version': self.registry.require(provider).manifest.version})
                    self.execute(con, '''INSERT INTO provider_contract_bindings(tenant_id,contract_id,provider,fingerprint)
                        VALUES(?,?,?,?)''', (tenant, contract.id, provider, fingerprint))
                for baseline in baselines:
                    self.execute(con, '''INSERT INTO contract_baselines
                        (tenant_id,contract_id,condition_id,result_json,captured_at) VALUES(?,?,?,?,?)''',
                        (tenant, contract.id, baseline.id, baseline.model_dump_json(), baseline.evidence.fetched_at.isoformat()))
                self.execute(con, '''INSERT INTO audit_events(tenant_id,action,object_type,object_id,metadata_json,created_at)
                    VALUES(?,'run.registered','contract',?,?,?)''',
                    (tenant, contract.id, canonical({'session_id': identifier, 'baselines': len(baselines)}), instant(now).isoformat()))
            self.execute(con, '''UPDATE assurance_sessions SET preparation_state=?,compiler_json=?,contract_id=?,
                providers_json=?,updated_at=? WHERE tenant_id=? AND id=?''',
                ('READY_FOR_EXECUTION' if contract else 'NEEDS_CLARIFICATION', result.model_dump_json(),
                 contract.id if contract else None, canonical(providers), now, tenant, identifier))

    def view(self, tenant, identifier):
        with self.transaction() as con:
            row = self.session_row(con, tenant, identifier)
            if row['preparation_state'] == 'PREPARING' and row['preparation_deadline'] <= self.now(con):
                self.execute(con, '''UPDATE assurance_sessions SET preparation_state='PREPARATION_FAILED',updated_at=?
                    WHERE tenant_id=? AND id=? AND preparation_state='PREPARING' ''', (self.now(con), tenant, identifier))
                row = self.session_row(con, tenant, identifier)
        contract = self.store.get_contract(tenant, row['contract_id']) if row['contract_id'] else None
        session = AssuranceSession(id=identifier, workspace=tenant, task=row['task'], state=row['preparation_state'],
            compiler=CompilationResult.model_validate_json(row['compiler_json']) if row['compiler_json'] else None,
            contract=contract, trusted_task_started_at=contract.task_started_at if contract else None,
            baselines=list(self.store.get_baselines(tenant, contract.id).values()) if contract else [],
            providers=json.loads(row['providers_json']), created_at=instant(row['created_at']), updated_at=instant(row['updated_at']))
        if not row['first_job_id']:
            return session
        first = self.get_job(tenant, row['first_job_id'])
        jobs = [first]
        history = None
        if first['state'] in {'COMPLETE', 'PARTIAL_FAILURE'}:
            history = self.history(tenant, first['receipt_id'])
            jobs += [self.get_job(tenant, item['job_id']) for item in history['attempts']]
            session.lineage = [ReceiptLink.model_validate(item) for item in history['receipts']]
            session.receipt = self.store.get_receipt(tenant, history['head_id'])
            session.can_reverify = history['can_reverify']
        current = jobs[-1]
        session.jobs = [DurableJob.model_validate(self.public(job)) for job in jobs]
        session.current_job_id = current['id']
        session.updated_at = instant(max(row['updated_at'], *(j['finished_at'] or j['started_at'] or j['created_at'] for j in jobs)))
        if current['state'] not in TERMINAL:
            session.state = 'REVERIFYING' if len(jobs) > 1 else 'VERIFYING'
        elif current['state'] in {'EXPIRED', 'INTERNAL_ERROR'}:
            session.state = 'UNKNOWN'
            # A previous receipt is historical, never the result of this failed attempt.
            session.receipt = None
        elif session.receipt:
            session.state = session.receipt.verdict.value
        if session.receipt:
            session.assurance = assurance_summary(session.receipt)
            session.signed_payload_b64 = base64.b64encode(ReceiptSigner._payload(session.receipt)).decode()
            session.verdict = session.receipt.verdict
            session.remediation = session.receipt.remediation
            session.evidence = [EvidenceAssurance(condition=r.id, provider=r.evidence.provider,
                evidence_class='browser_ui' if r.evidence.provenance else ('unavailable' if r.evidence.provider == 'unresolved' else 'provider_observation'),
                assurance_level='lower_than_authoritative_api' if r.evidence.provenance else ('unavailable' if r.evidence.provider == 'unresolved' else 'provider_declared'),
                provenance=r.evidence.provenance) for r in session.receipt.results]
        return AssuranceSession.model_validate(session.model_dump())

    def verify_session(self, tenant, identifier, key, req, *, reverify=False):
        callback = None
        if req.callback_id:
            endpoint = self.app.state.job_callbacks.get(tenant, req.callback_id)
            if not endpoint:
                raise RecoveryError('callback_not_configured', 422)
            callback = req.callback_id, endpoint['fingerprint']
        request_hash = digest(canonical({'reverify': reverify, **req.model_dump()}))
        with self.transaction() as con:
            session = self.session_row(con, tenant, identifier, lock=True)
            old = self._row(self.execute(con, '''SELECT * FROM assurance_requests
                WHERE tenant_id=? AND session_id=? AND idempotency_hash=?''', (tenant, identifier, digest(key))))
            if old:
                if old['request_hash'] != request_hash:
                    raise IdempotencyConflict
                return old['job_id']
            if session['preparation_state'] != 'READY_FOR_EXECUTION':
                raise RecoveryError('session_not_ready')
            if any(not self.registry.get(p['provider']) or self.registry.require(p['provider']).fingerprint != p['fingerprint']
                   for p in json.loads(session['providers_json'])):
                raise RecoveryError('provider_declaration_changed')
            job_key = 'assurance:' + identifier + ':' + digest(key)
            if reverify:
                first = self.row(con, tenant, session['first_job_id']) if session['first_job_id'] else None
                if not first or first['state'] not in {'COMPLETE', 'PARTIAL_FAILURE'}:
                    raise RecoveryError('initial_verification_incomplete')
                chain = self.ensure(con, tenant, first['receipt_id'])
                job, _ = self._admit(con, chain, req.previous_receipt_id, job_key, request_hash, req.deadline_seconds, callback)
            elif session['first_job_id']:
                if session['verification_hash'] != request_hash:
                    raise IdempotencyConflict
                job = self.row(con, tenant, session['first_job_id'])
            else:
                compilation = CompilationResult.model_validate_json(session['compiler_json'])
                baselines = {b['condition_id']: b['result_json'] for b in self.execute(con,
                    'SELECT condition_id,result_json FROM contract_baselines WHERE tenant_id=? AND contract_id=?',
                    (tenant, session['contract_id'])).fetchall()}
                from .domain import ConditionResult
                job, _ = self.create_in_transaction(con, tenant, job_key, request_hash, compilation.contract,
                    {k: ConditionResult.model_validate_json(v) for k, v in baselines.items()}, 'registered', req.deadline_seconds, callback)
                self.execute(con, '''UPDATE assurance_sessions SET first_job_id=?,verification_hash=?,updated_at=?
                    WHERE tenant_id=? AND id=?''', (job['id'], request_hash, self.now(con), tenant, identifier))
            self.execute(con, '''INSERT INTO assurance_requests
                (tenant_id,session_id,idempotency_hash,request_hash,job_id) VALUES(?,?,?,?,?)''',
                (tenant, identifier, digest(key), request_hash, job['id']))
            return job['id']
