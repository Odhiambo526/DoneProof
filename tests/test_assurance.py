import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from doneproof.adapters.base import ProviderAdapter, ProviderObservation
from doneproof.app import create_app
from doneproof.assurance_models import PrepareSession
from doneproof.compilation_models import CompilationResult, ContractQuality
from doneproof.signing import ReceiptSigner
from doneproof.worker import VerificationWorker
from tests.test_compilation import ReadyResolver

A = {'X-DoneProof-Key': 'key-a', 'Idempotency-Key': 'outcome-1842'}
B = {'X-DoneProof-Key': 'key-b', 'Idempotency-Key': 'outcome-1842'}
TASK = {'task': 'Close issue #12 in acme/api'}


class FixtureIssue(ProviderAdapter):
    def __init__(self):
        self.state = 'open'
        self.calls = 0
        self.unknown = False

    async def observe(self, selector, context):
        self.calls += 1
        return ProviderObservation({'number': 12, 'state': self.state}, indeterminate=self.unknown)


@pytest.fixture
def assurance(connection_settings):
    provider = FixtureIssue()
    app = create_app(connection_settings, {'github': provider})
    app.state.compiler.resolver = ReadyResolver()
    return app, TestClient(app), VerificationWorker(app.state.store, app.state.engine), provider


def prepare(client, headers=A):
    response = client.post('/v1/assurance/sessions', json=TASK, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def complete(app, worker, session):
    asyncio.run(worker.run_until_terminal('tenant-a', session['current_job_id']))
    return app.state.assurance.view('tenant-a', session['id'])


def test_lifecycle_independent_repair_chain_and_idempotency(assurance):
    app, client, worker, provider = assurance
    session = prepare(client)
    assert session['state'] == 'READY_FOR_EXECUTION'
    assert session['trusted_task_started_at'] == session['contract']['task_started_at']
    assert session['baselines'][0]['status'] == 'FAIL'
    assert session['providers'][0]['fingerprint'] == app.state.providers.require('github').fingerprint
    assert prepare(client)['id'] == session['id'] and provider.calls == 1
    url = '/v1/assurance/sessions/' + session['id']
    first = client.post(url + '/verify', json={}, headers=A).json()
    duplicate = client.post(url + '/verify', json={}, headers={**A, 'Idempotency-Key': 'different'}).json()
    assert duplicate['current_job_id'] == first['current_job_id']
    failed = complete(app, worker, first)
    assert failed.verdict == 'FAILED' and failed.remediation and failed.can_reverify
    original = failed.receipt.model_dump_json()
    # The customer fixture changes externally; guidance never enters the adapter.
    provider.state = 'closed'
    repaired = client.post(url + '/reverify', json={'previous_receipt_id': failed.receipt.receipt_id}, headers=A)
    assert repaired.status_code == 409  # key already used for initial verify
    headers = {**A, 'Idempotency-Key': 'repair-1'}
    repaired = client.post(url + '/reverify', json={'previous_receipt_id': failed.receipt.receipt_id}, headers=headers)
    assert repaired.status_code == 202, repaired.text
    final = complete(app, worker, repaired.json())
    assert final.verdict == 'VERIFIED' and len(final.lineage) == 2
    assert final.receipt.previous_receipt_id == failed.receipt.receipt_id
    assert ReceiptSigner.verify_trusted(final.receipt, app.state.signer.public_key_b64)
    assert app.state.store.get_receipt('tenant-a', failed.receipt.receipt_id).model_dump_json() == original
    retry = client.post(url + '/reverify', json={'previous_receipt_id': failed.receipt.receipt_id}, headers=headers)
    assert retry.status_code == 202 and len(retry.json()['jobs']) == 2


def test_concurrent_double_preparation_and_verify(assurance):
    app, client, _, provider = assurance
    with ThreadPoolExecutor(max_workers=4) as pool:
        sessions = list(pool.map(lambda _: prepare(client), range(4)))
    assert len({s['id'] for s in sessions}) == 1 and provider.calls == 1
    url = '/v1/assurance/sessions/' + sessions[0]['id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda n: client.post(url + '/verify', json={},
            headers={**A, 'Idempotency-Key': str(n)}), range(4)))
    assert all(r.status_code == 202 for r in responses)
    assert len({r.json()['current_job_id'] for r in responses}) == 1


@pytest.mark.parametrize('suffix', ['', '/verify', '/reverify', '/receipt'])
def test_cross_tenant_session_is_not_found(assurance, suffix):
    _, client, _, _ = assurance
    session = prepare(client)
    url = '/v1/assurance/sessions/' + session['id'] + suffix
    response = client.post(url, json={'previous_receipt_id': 'vr_' + 'a' * 32} if suffix == '/reverify' else {}, headers=B) if suffix in {'/verify', '/reverify'} else client.get(url, headers=B)
    assert response.status_code == 404


@pytest.mark.parametrize('field', ['observations', 'screenshot', 'page_state', 'success', 'reasoning', 'tool_output'])
def test_executor_input_rejected_without_echo(assurance, field):
    _, client, _, _ = assurance
    marker = 'private-customer-body-sentinel'
    assert marker not in client.post('/v1/assurance/sessions', json={**TASK, field: marker}, headers=A).text
    assert client.post('/v1/assurance/sessions', json={**TASK, 'context': {field: marker}}, headers=A).status_code == 422
    session = prepare(client)
    response = client.post('/v1/assurance/sessions/' + session['id'] + '/verify', json={field: marker}, headers=A)
    assert response.status_code == 422 and marker not in response.text


def test_clarification_and_unknown_baseline_never_ready(assurance):
    _, client, _, provider = assurance
    response = client.post('/v1/assurance/sessions', json={'task': 'Create a task in Notion'}, headers=A).json()
    assert response['state'] == 'NEEDS_CLARIFICATION' and response['contract'] is None
    assert response['compiler']['status'] == 'unsupported_provider'
    provider.unknown = True
    session = prepare(client, {**A, 'Idempotency-Key': 'unknown-baseline'})
    assert session['state'] == 'NEEDS_CLARIFICATION' and session['contract'] is None


def test_interrupted_preparation_cannot_move_boundary(assurance):
    app, client, _, provider = assurance
    row, _ = app.state.assurance.reserve('tenant-a', A['Idempotency-Key'], PrepareSession(**TASK))
    with app.state.assurance.transaction() as con:
        app.state.assurance.execute(con, 'UPDATE assurance_sessions SET preparation_deadline=0 WHERE id=?', (row['id'],))
    assert prepare(client)['state'] == 'PREPARATION_FAILED'
    assert provider.calls == 0
    assert client.post('/v1/assurance/sessions/' + row['id'] + '/verify', json={}, headers=A).status_code == 409


def test_malformed_compiler_result_fails_closed(assurance):
    app, client, _, _ = assurance
    async def malformed(*args):
        return CompilationResult(status='valid_contract', contract_quality=ContractQuality(confidence=1))
    app.state.compiler.compile = malformed
    assert prepare(client)['state'] == 'PREPARATION_FAILED'


def test_cancelled_job_never_reuses_previous_verdict(assurance):
    app, client, worker, _ = assurance
    session = prepare(client)
    url = '/v1/assurance/sessions/' + session['id']
    first = client.post(url + '/verify', json={}, headers=A).json()
    failed = complete(app, worker, first)
    repair = client.post(url + '/reverify', json={'previous_receipt_id': failed.receipt.receipt_id},
                         headers={**A, 'Idempotency-Key': 'repair'}).json()
    client.post('/v1/jobs/' + repair['current_job_id'] + '/cancel', headers=A)
    result = client.get(url, headers=A).json()
    assert result['state'] == 'UNKNOWN' and result['receipt'] is None and result['verdict'] is None
    assert len(result['lineage']) == 1
    assert client.get(url + '/receipt', headers=A).status_code == 409
