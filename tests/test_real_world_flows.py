import asyncio
import base64
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from doneproof.adapters.base import ObservationContext
from doneproof.adapters.github import GitHubAdapter
from doneproof.adapters.gmail import GmailAdapter
from doneproof.assurance_models import AssuranceSession
from doneproof.connections import ManagedAdapter
from doneproof.domain import VerificationReceipt
from doneproof.rehearsal import Rehearsal, RehearsalFailure, new_state
from doneproof.retries import durable_observation
from doneproof.signing import ReceiptSigner
from tests.connection_helpers import ACCESS, ADMIN_A, ADMIN_B, begin, finish, seed
from tests.test_assurance import A, assurance, complete  # noqa: F401
from tests.test_compilation import compiler


@pytest.mark.parametrize('problem', ['foreign_app', 'suspended', 'incomplete', 'write_permission'])
def test_github_onboarding_rejects_wrong_or_unusable_installation(connection_app, problem):
    app, stub = connection_app
    install = stub.installations['installations'][0]
    if problem == 'foreign_app':
        install['app_slug'] = 'another-app'
    elif problem == 'suspended':
        install['suspended_at'] = '2026-01-01T00:00:00Z'
    elif problem == 'incomplete':
        stub.installations['total_count'] = 101
    else:
        install['permissions']['contents'] = 'write'
    client = TestClient(app, base_url='https://testserver')
    query, _ = begin(client, 'github')
    assert finish(client, query, 'github').headers['location'].endswith('authorization-failed')
    assert app.state.connections.capability('tenant-a', 'github') == 'configuration_required'


def test_private_repository_discovery_requires_tenant_admin_and_managed_oauth(connection_app):
    app, _ = connection_app
    client = TestClient(app, base_url='https://testserver')
    query, _ = begin(client, 'github')
    assert finish(client, query, 'github').headers['location'].endswith('#connected')
    row = app.state.connections.db.get('tenant-a', provider='github')
    path = '/v1/connections/' + row['id'] + '/repositories'
    assert client.get(path).status_code == 401
    assert client.get(path, headers=ADMIN_B).status_code == 404
    response = client.get(path, headers=ADMIN_A)
    assert response.status_code == 200 and response.json()['evidence'] is False
    assert response.json()['repositories'] == [{'id': 1234, 'repository': 'example/project', 'private': True, 'installation_id': 1}]
    assert ACCESS not in response.text and 'refresh_token' not in response.text


def test_private_github_anonymous_404_then_managed_authorized_read(connection_app):
    app, stub = connection_app
    adapter = ManagedAdapter(app.state.connections, 'github')
    ctx = ObservationContext('tenant-a', 'registered', '2026-01-01T00:00:00Z')
    selector = {'repo': 'example/project', 'kind': 'issue', 'number': 1}
    stub.message_status = 404
    assert asyncio.run(adapter.observe(selector, ctx)).indeterminate
    seed(app.state.connections, 'github')
    stub.message_status = 200
    result = asyncio.run(adapter.observe(selector, ctx))
    assert not result.indeterminate and result.state['state'] == 'closed'
    assert stub.requests[-1].headers['authorization'] == 'Bearer ' + ACCESS
    stub.message_status = 401
    assert asyncio.run(adapter.observe(selector, ctx)).indeterminate
    assert app.state.connections.db.get('tenant-a', provider='github')['state'] == 'reconnect_required'


@pytest.mark.parametrize('problem', ['wrong_number', 'pull_request', 'cached'])
def test_github_identity_and_cached_responses_are_not_authoritative(problem):
    data = {'number': 9, 'state': 'closed'}
    headers = {}
    if problem == 'wrong_number':
        data['number'] = 10
    elif problem == 'pull_request':
        data['pull_request'] = {}
    else:
        headers['Age'] = '60'
    adapter = GitHubAdapter(allow_env=False, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data, headers=headers)))
    result = asyncio.run(adapter.observe({'repo': 'acme/api', 'kind': 'issue', 'number': 9},
                                        ObservationContext('tenant-a', 'run', '2026-01-01T00:00:00Z')))
    assert result.indeterminate and result.state is None


def test_durable_managed_response_hook_cannot_bypass_body_limit(connection_app):
    app, stub = connection_app
    service = app.state.connections
    seed(service, 'github')
    consumed = []
    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                consumed.append(1)
                yield b'x' * 65536
    service.providers.transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=Endless()))
    async def run():
        token = durable_observation.set(True)
        try:
            return await ManagedAdapter(service, 'github').observe({'repo': 'acme/api', 'kind': 'issue', 'number': 1},
                ObservationContext('tenant-a', 'run', '2026-01-01T00:00:00Z'))
        finally:
            durable_observation.reset(token)
    assert asyncio.run(run()).indeterminate
    assert len(consumed) <= 33


@pytest.mark.parametrize('labels,expected', [(['DRAFT'], 'draft'), ([], 'other'), (['SENT'], 'sent')])
def test_gmail_queued_or_draft_never_implies_sent(labels, expected):
    assert GmailAdapter._normalize({'id': 'message', 'labelIds': labels})['location'] == expected


def test_gmail_conflicting_labels_and_fixed_draft_id_fail_closed(settings):
    with pytest.raises(ValueError):
        GmailAdapter._normalize({'id': 'message', 'labelIds': ['DRAFT', 'SENT']})
    service, _ = compiler(settings)
    result = asyncio.run(service.compile('Send Gmail draft msg17', {}, 'tenant-a'))
    assert result.status == 'missing_identifier' and result.contract is None
    assert result.clarification_requirements[0].code == 'gmail_draft_identity_changes'


def test_explicit_transition_policy_and_no_change_are_not_certified(assurance):  # noqa: F811
    app, client, worker, provider = assurance
    provider.state = 'closed'
    session = client.post('/v1/assurance/sessions', headers=A,
        json={'task': 'Check issue #12 in acme/api is closed', 'require_transition': True}).json()
    assert session['baselines'][0]['status'] == 'PASS'
    job = client.post('/v1/assurance/sessions/' + session['id'] + '/verify', headers=A, json={}).json()
    result = complete(app, worker, job)
    assert result.verdict == 'FAILED' and result.assurance.level == 'registered'
    assert result.assurance.transitions_proven == 0


def test_rehearsal_negative_external_repair_link_and_pins(assurance, monkeypatch):  # noqa: F811
    app, client, worker, provider = assurance
    def transport(request):
        response = client.request(request.method, request.url.path, headers=dict(request.headers), content=request.content)
        if request.method == 'POST' and request.url.path.endswith(('/verify', '/reverify')) and response.status_code == 202:
            job = response.json()['current_job_id']
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(asyncio.run, worker.run_until_terminal('tenant-a', job)).result()
        return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))
    runner = Rehearsal('https://testserver', 'key-a', {app.state.signer.key_id: app.state.signer.public_key_b64},
                       transport=httpx.MockTransport(transport))
    # This test exercises client invariants against controlled provider fixtures.
    # The deployment gate is tested separately and never bypassed by the CLI.
    monkeypatch.setattr(runner, 'check_deployment', lambda: {'fixture': True})
    state = new_state('github', repo='acme/api', number=12)
    try:
        runner.prepare(state)
        runner.prepare(state)
        runner.negative(state)
        original = state['receipts'][0].copy()
        provider.state = 'closed'  # External fixture actor; never an input to DoneProof.
        result = runner.positive(state)
        assert state['stage'] == 'COMPLETE' and state['receipts'][0] == original
        assert result.assurance.level == 'transition_assured'
        runner.positive(state)
        assert len(state['receipts']) == 2
        forged = result.model_copy(deep=True)
        forged.assurance.level = 'submitted'
        with pytest.raises(ValueError):
            AssuranceSession.model_validate(forged.model_dump())
        runner.pins = {}
        with pytest.raises(RehearsalFailure, match='invalid_pinned_signature'):
            runner.inspect_receipt(state, result, positive=True)
    finally:
        runner.close()


def test_rehearsal_refuses_ephemeral_deployment_and_unpinned_issuer():
    responses = {'/ready': {'storage_backend': 'sqlite', 'durable_storage': False, 'environment': 'test'}}
    runner = Rehearsal('https://testserver', 'test-key', {'pin': 'untrusted'}, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=responses[req.url.path])))
    try:
        with pytest.raises(RehearsalFailure, match='persistent_postgresql_required'):
            runner.check_deployment()
        responses['/ready'] = {'storage_backend': 'postgresql', 'durable_storage': True, 'environment': 'staging'}
        responses['/v1/signing-key'] = {'key_id': 'different', 'public_key': 'different'}
        with pytest.raises(RehearsalFailure, match='issuer_pin_mismatch'):
            runner.check_deployment()
    finally:
        runner.close()


def test_rotation_preserves_historical_pins_and_rejects_replacement(settings):
    fixtures = json.loads((__import__('pathlib').Path(__file__).parents[1] / 'sdk/typescript/test/fixtures.json').read_text())['receipts']
    rotated = ReceiptSigner(replace(settings, signing_seed_b64=base64.b64encode(b'r' * 32).decode()))
    for fixture in fixtures:
        receipt = VerificationReceipt.model_validate(fixture['receipt'])
        original = receipt.model_dump_json()
        assert ReceiptSigner.verify_trusted(receipt, fixture['pinned_public_key'])
        assert not ReceiptSigner.verify_trusted(receipt, rotated.public_key_b64)
        assert receipt.model_dump_json() == original


def test_console_exposes_preparation_permissions_and_pinned_trust(auth_settings):
    from doneproof.app import create_app
    client = TestClient(create_app(auth_settings))
    assert 'Prepare before your agent acts' in client.get('/console').text
    script = client.get('/console/sessions.js').text
    assert 'require_transition' in script and "crypto.subtle.verify('Ed25519'" in script
    assert 'Show authorized repositories' in client.get('/connections.js').text


def test_rehearsal_state_lock_rejects_concurrent_invocation(tmp_path):
    from doneproof.rehearsal import state_lock
    path = tmp_path / 'state.json'
    with state_lock(path):
        with pytest.raises(RehearsalFailure, match='state_locked'):
            with state_lock(path):
                pytest.fail('Concurrent state mutation admitted')
    with state_lock(path):
        pass


@pytest.mark.parametrize('endpoint', ['installations', 'repositories'])
def test_managed_repository_invalid_json_is_sanitized(connection_settings, endpoint):
    from doneproof.adapters.builtin_oauth import BuiltinOAuthProvider
    from doneproof.provider_errors import ProviderFailure
    def response(request):
        if request.url.path == '/user/installations' and endpoint == 'repositories':
            return httpx.Response(200, json={'total_count': 1, 'installations': [{
                'id': 1, 'app_slug': 'doneproof-test', 'permissions': {'issues': 'read', 'pull_requests': 'read'}}]})
        return httpx.Response(200, text='secret-sentinel-invalid-json')
    provider = BuiltinOAuthProvider(connection_settings, httpx.MockTransport(response))
    with pytest.raises(ProviderFailure) as error:
        asyncio.run(provider.repositories({'kind': 'oauth', 'access_token': 'fixture-token'}))
    assert 'secret-sentinel' not in str(error.value)


def test_gmail_new_message_identity_proves_sent_transition(settings):
    from doneproof.engine import VerificationEngine
    from tests.test_gmail import START, contract, gmail_message
    c = contract()
    c.postconditions = c.postconditions[:1]
    c.postconditions[0].require_change = True
    current = gmail_message(mid='draft-id', labels=['DRAFT'])
    def response(request):
        if request.url.path.endswith('/messages'):
            return httpx.Response(200, json={'messages': [{'id': current['id']}]})
        return httpx.Response(200, json=current)
    adapter = GmailAdapter(replace(settings, gmail_access_token='fixture-readonly-token'),
                           transport=httpx.MockTransport(response))
    engine = VerificationEngine({'gmail': adapter}, ReceiptSigner(settings))
    async def workflow():
        baseline = await engine.snapshot(c, 'tenant-a')
        assert baseline[0].status == 'FAIL'
        first = await engine.verify(c, 'tenant-a', 'registered', {b.id: b for b in baseline})
        assert first.verdict == 'FAILED'
        current.update(gmail_message(mid='new-sent-id', labels=['SENT']))
        final = await engine.verify(c, 'tenant-a', 'registered', {b.id: b for b in baseline})
        assert final.verdict == 'VERIFIED' and final.results[0].baseline_status == 'FAIL'
        assert final.results[0].evidence.fetched_at > START
        assert ReceiptSigner.verify_trusted(final, engine.signer.public_key_b64)
    asyncio.run(workflow())


@pytest.mark.parametrize('problem', ['cached_search', 'cached_detail', 'wrong_identity'])
def test_gmail_discovery_rejects_stale_or_crossed_identity(settings, problem):
    from tests.test_gmail import START, gmail_message
    def response(request):
        if request.url.path.endswith('/messages'):
            return httpx.Response(200, json={'messages': [{'id': 'm1'}]},
                                  headers={'Age': '20'} if problem == 'cached_search' else {})
        return httpx.Response(200, json=gmail_message(mid='wrong' if problem == 'wrong_identity' else 'm1'),
                              headers={'Age': '20'} if problem == 'cached_detail' else {})
    adapter = GmailAdapter(replace(settings, gmail_access_token='fixture-token'), transport=httpx.MockTransport(response))
    result = asyncio.run(adapter.observe({'subject': 'Invoice', 'to': 'alice@example.com'},
                                        ObservationContext('tenant-a', 'registered', START)))
    assert result.indeterminate and result.state is None


def test_sdk_resumes_preparing_session_without_second_mutation(assurance):  # noqa: F811
    _, client, _, provider = assurance
    ready = client.post('/v1/assurance/sessions', headers=A,
                        json={'task': 'Close issue #12 in acme/api'}).json()
    preparing = {**ready, 'state': 'PREPARING', 'contract': None, 'compiler': None,
                 'trusted_task_started_at': None, 'baselines': [], 'providers': []}
    seen = []
    def respond(request):
        seen.append(request.method)
        return httpx.Response(200, json=preparing if len(seen) == 1 else ready)
    from doneproof import DoneProof
    with DoneProof(api_key='key-a', base_url='https://testserver', transport=httpx.MockTransport(respond)) as dp:
        result = dp.assurance.prepare(task=ready['task'], idempotency_key='stable')
    assert result.id == ready['id'] and result.state == 'READY_FOR_EXECUTION'
    assert seen == ['POST', 'GET'] and provider.calls == 1
