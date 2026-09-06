"""Resumable client-side rehearsal. External business actions are never performed."""
from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from . import DoneProof
from .assurance_models import assurance_summary
from .signing import ReceiptSigner


class RehearsalFailure(Exception):
    pass


def require(value, code):
    if not value:
        raise RehearsalFailure(code)


@contextmanager
def state_lock(path):
    """Concurrent CLI invocations must not replace the persisted retry identity.

    A process crash deliberately leaves the lock for an operator to inspect.
    Automatically expiring it could race a slow, still-running preparation.
    """
    lock = Path(str(path) + '.lock')
    try:
        stream = lock.open('x')
    except FileExistsError:
        raise RehearsalFailure('state_locked_inspect_owner_before_removing_lock') from None
    try:
        with stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink()


def save(path, state):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(state, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def new_state(provider, *, repo=None, number=None, to=None, subject=None):
    identifier = uuid4().hex
    if provider == 'github':
        require(isinstance(repo, str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo), 'invalid_repository')
        require(type(number) is int and number > 0, 'invalid_issue_number')
        task = f'Close issue #{number} in {repo}'
        target = {'repo': repo, 'kind': 'issue', 'number': number}
    else:
        require(provider == 'gmail', 'unsupported_rehearsal_provider')
        require(isinstance(to, str) and re.fullmatch(r'[^\s";]+@[^\s";]+', to), 'invalid_recipient')
        require(isinstance(subject, str) and 1 <= len(subject) <= 120 and not re.search(r'["\r\n;]', subject), 'invalid_subject')
        subject = f'{subject} [{identifier}]'
        task = f'Send email to {to} with subject "{subject}"'
        target = {'to': to, 'subject': subject}
    return {'version': 1, 'id': identifier, 'provider': provider, 'target': target,
            'task': task, 'stage': 'NEW', 'session_id': None, 'negative_receipt_id': None,
            'receipts': [], 'external_action_performed_by_doneproof': False}


class Rehearsal:
    def __init__(self, base_url, key, pins, *, allow_loopback=False, transport=None):
        parts = urlsplit(base_url)
        require(not parts.username and not parts.password and not parts.query and not parts.fragment,
                'invalid_api_origin')
        require(parts.scheme == 'https' or allow_loopback and parts.scheme == 'http'
                and parts.hostname in {'localhost', '127.0.0.1', '::1'}, 'https_required')
        require(isinstance(pins, dict) and bool(pins), 'trusted_pins_required')
        self.pins = pins
        self.http = httpx.Client(base_url=base_url, headers={'X-DoneProof-Key': key}, timeout=20,
                                 follow_redirects=False, trust_env=False, transport=transport)
        self.dp = DoneProof(api_key=key, base_url=base_url, transport=transport)

    def close(self):
        self.dp.close()
        self.http.close()

    def check_deployment(self):
        response = self.http.get('/ready')
        require(response.status_code == 200, 'api_not_ready')
        ready = response.json()
        require(ready.get('storage_backend') == 'postgresql' and ready.get('durable_storage') is True,
                'persistent_postgresql_required')
        require(ready.get('environment') in {'staging', 'production'}, 'stable_staging_or_production_required')
        public = self.http.get('/v1/signing-key')
        require(public.status_code == 200, 'issuer_unavailable')
        issuer = public.json()
        require(self.pins.get(issuer.get('key_id')) == issuer.get('public_key'), 'issuer_pin_mismatch')
        return {'revision': ready.get('revision'), 'schema_version': ready.get('schema_version'),
                'environment': ready['environment'], 'key_id': issuer['key_id']}

    def validate_session(self, state, session):
        require(session.contract and session.trusted_task_started_at, 'trusted_registration_required')
        require(session.task == state['task'] and session.contract.task == state['task'], 'session_task_mismatch')
        conditions = session.contract.postconditions
        require(conditions and all(c.provider == state['provider'] and c.required and c.require_change for c in conditions),
                'required_transition_contract_missing')
        for condition in conditions:
            require(all(condition.selector.get(k) == v for k, v in state['target'].items()), 'target_binding_mismatch')
        goal = ('state', 'closed') if state['provider'] == 'github' else ('location', 'sent')
        require(any(c.predicate.op == 'eq' and (c.predicate.path, c.predicate.expected) == goal for c in conditions),
                'intended_outcome_missing')
        require({b.id for b in session.baselines} == {c.id for c in conditions}, 'baseline_missing')
        require(all(b.status == 'FAIL' for b in session.baselines), 'baseline_not_false')
        require(all(b.evidence.fetched_at >= session.trusted_task_started_at for b in session.baselines), 'stale_baseline')
        return session

    def prepare(self, state):
        state['deployment'] = self.check_deployment()
        session = self.dp.assurance.prepare(task=state['task'], idempotency_key='rehearsal:' + state['id'],
                                            require_transition=True)
        require(session.state == 'READY_FOR_EXECUTION', 'connection_or_clarification_required')
        self.validate_session(state, session)
        require(not state['session_id'] or state['session_id'] == session.id, 'preparation_replay_changed_session')
        state['session_id'], state['stage'] = session.id, 'PREPARED'
        return session

    def inspect_receipt(self, state, session, *, positive):
        self.validate_session(state, session)
        receipt = session.receipt
        require(receipt is not None, 'receipt_missing')
        pin = self.pins.get(receipt.key_id)
        require(pin and ReceiptSigner.verify_trusted(receipt, pin), 'invalid_pinned_signature')
        contract_bytes = json.dumps(session.contract.model_dump(mode='json'), sort_keys=True,
                                    separators=(',', ':'), ensure_ascii=False).encode()
        require(receipt.contract_hash == hashlib.sha256(contract_bytes).hexdigest(), 'receipt_contract_mismatch')
        require(receipt.assurance_level == 'registered', 'submitted_receipt_refused')
        require({r.id for r in receipt.results} == {c.id for c in session.contract.postconditions}, 'condition_set_mismatch')
        by_id = {c.id: c for c in session.contract.postconditions}
        require(all(r.predicate == by_id[r.id].predicate and r.required and r.transition_required
                    and r.evidence.provider == state['provider'] and r.evidence.provenance is None
                    and r.evidence.selector == by_id[r.id].selector for r in receipt.results), 'receipt_condition_mismatch')
        require(all(r.evidence.fetched_at > session.trusted_task_started_at for r in receipt.results), 'stale_observation')
        require(all(r.status != 'UNKNOWN' for r in receipt.results), 'indeterminate_provider_evidence')
        if positive:
            require(receipt.verdict == 'VERIFIED' and assurance_summary(receipt).level == 'transition_assured',
                    'transition_not_proven')
            require(receipt.previous_receipt_id == state['negative_receipt_id'], 'receipt_link_mismatch')
            require(any(link.receipt_id == state['negative_receipt_id'] for link in session.lineage), 'history_truncated')
            old = self.http.get('/v1/receipts/' + state['negative_receipt_id'])
            require(old.status_code == 200, 'original_receipt_missing')
            from .domain import VerificationReceipt
            prior = VerificationReceipt.model_validate(old.json())
            require(prior.receipt_hash == state['receipts'][0]['receipt_hash']
                    and ReceiptSigner.verify_trusted(prior, self.pins[prior.key_id]), 'original_receipt_changed')
        else:
            require(receipt.verdict in {'FAILED', 'PARTIAL'} and any(r.status == 'FAIL' for r in receipt.results),
                    'negative_control_did_not_fail')
        return {'receipt_id': receipt.receipt_id, 'receipt_hash': receipt.receipt_hash,
                'key_id': receipt.key_id, 'verdict': receipt.verdict.value,
                'assurance': assurance_summary(receipt).model_dump(), 'signature_valid': True}

    def negative(self, state, timeout=120):
        self.check_deployment()
        require(state['stage'] in {'PREPARED', 'NEGATIVE'}, 'prepare_before_external_execution')
        result = self.dp.assurance.verify(state['session_id'], wait=True, timeout=timeout)
        record = self.inspect_receipt(state, result, positive=False)
        state['negative_receipt_id'] = record['receipt_id']
        state['receipts'], state['stage'] = [record], 'NEGATIVE'
        return result

    def positive(self, state, timeout=120):
        self.check_deployment()
        require(state['stage'] in {'NEGATIVE', 'COMPLETE'}, 'negative_control_required')
        result = self.dp.assurance.reverify(state['session_id'], previous_receipt_id=state['negative_receipt_id'],
            idempotency_key='rehearsal:' + state['id'] + ':positive', wait=True, timeout=timeout)
        record = self.inspect_receipt(state, result, positive=True)
        state['receipts'], state['stage'] = [state['receipts'][0], record], 'COMPLETE'
        return result
