import asyncio
import base64
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator

from doneproof import DoneProof
from doneproof import store as store_module
from doneproof.assurance_models import AssuranceSession
from doneproof.domain import CompletionContract, VerificationReceipt
from doneproof.signing import ReceiptSigner
from doneproof.store import Store
from tests.test_assurance import TASK, A, assurance, prepare  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('script', ['generate_sync_sdk.py', 'sdk_schemas.py', 'sdk_fixtures.py'])
def test_shared_sdk_artifacts_are_current(script):
    subprocess.run([sys.executable, str(ROOT / 'scripts' / script), '--check'], cwd=ROOT, check=True, capture_output=True)


def test_openapi_references_and_session_roundtrip(assurance):  # noqa: F811
    app, client, _, _ = assurance
    schema = app.openapi()
    def walk(value):
        if isinstance(value, dict):
            if '$ref' in value and value['$ref'].startswith('#/'):
                target = schema
                for key in value['$ref'][2:].split('/'):
                    target = target[key.replace('~1', '/').replace('~0', '~')]
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(schema)
    for path, methods in {
        '/v1/assurance/sessions': ['post'], '/v1/assurance/sessions/{identifier}': ['get'],
        '/v1/assurance/sessions/{identifier}/verify': ['post'],
        '/v1/assurance/sessions/{identifier}/reverify': ['post'],
        '/v1/assurance/sessions/{identifier}/receipt': ['get'],
        '/v2/connections/{connection_id}/disconnect': ['post'],
    }.items():
        assert all(method in schema['paths'][path] for method in methods)
    result = prepare(client)
    Draft202012Validator(AssuranceSession.model_json_schema()).validate(result)
    assert AssuranceSession.model_validate(result).model_dump(mode='json') == result


def test_schema_7_upgrade_preserves_schema_6_receipt_bytes(connection_settings, monkeypatch):
    legacy = json.loads((ROOT / 'tests/fixtures/legacy_recovery_receipt.json').read_text())
    with monkeypatch.context() as patch:
        patch.setattr(store_module, 'migrate_assurance', lambda con: None)
        before = Store(connection_settings.storage_dsn)
    before.save_contract('tenant-a', CompletionContract.model_validate(legacy['contract']))
    before.save_receipt('tenant-a', VerificationReceipt.model_validate(legacy['receipt']))
    from doneproof.connection_store import ConnectionStore
    db = ConnectionStore(before)
    with db.transaction() as con:
        original = db._row(db.execute(con, 'SELECT body_json,signature FROM receipts WHERE tenant_id=?', ('tenant-a',)))
        if db.pg:
            con.execute('DELETE FROM schema_migrations WHERE version=7')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: Store(connection_settings.storage_dsn), range(4)))
    with db.transaction() as con:
        assert db._row(db.execute(con, 'SELECT body_json,signature FROM receipts WHERE tenant_id=?', ('tenant-a',))) == original
        assert db.execute(con, 'SELECT COUNT(*) AS n FROM assurance_sessions').fetchone()['n'] == 0
        if db.pg:
            assert [r['version'] for r in con.execute('SELECT version FROM schema_migrations ORDER BY version')] == list(range(1, 8))


def test_disconnect_retry_cannot_disable_reauthorized_connection(connection_app):
    app, _ = connection_app
    db = app.state.connections.db
    connection = db.ensure('tenant-a', 'github')
    with DoneProof(api_key='admin-a', base_url='https://testserver', transport=httpx.ASGITransport(app)) as dp:
        status = dp.connection_status(connection['id'])
        result = dp.disconnect(status.id, expected_revision=status.revision, idempotency_key='disconnect-1')
        assert result.state == 'disabled'
        from fastapi.testclient import TestClient

        from tests.connection_helpers import begin, finish
        browser = TestClient(app, base_url='https://testserver')
        query, _ = begin(browser, 'github')
        assert finish(browser, query, 'github').status_code == 303
        current = db.get('tenant-a', connection_id=status.id)
        assert current['state'] == 'connected'
        retried = dp.disconnect(status.id, expected_revision=status.revision, idempotency_key='disconnect-1')
        assert retried.state == 'connected'
        assert db.get('tenant-a', connection_id=status.id)['revision'] == current['revision']
        from doneproof.sdk_common import ConflictError
        with pytest.raises(ConflictError):
            dp.disconnect(status.id, expected_revision=status.revision, idempotency_key='different-operation')


def test_stale_provider_and_reverify_race_fail_closed(assurance):  # noqa: F811
    app, client, _, _ = assurance
    session = prepare(client)
    url = '/v1/assurance/sessions/' + session['id']
    response = client.post(url + '/reverify', headers=A, json={'previous_receipt_id': 'vr_' + 'a' * 32})
    assert response.status_code == 409
    db = app.state.assurance
    with db.transaction() as con:
        providers = session['providers']
        providers[0]['fingerprint'] = '0' * 64
        db.execute(con, 'UPDATE assurance_sessions SET providers_json=? WHERE tenant_id=? AND id=?',
                   (json.dumps(providers), 'tenant-a', session['id']))
    assert client.post(url + '/verify', headers=A, json={}).status_code == 409


def test_browser_session_exposes_lower_assurance_and_exact_payload(connection_settings, monkeypatch):
    from tests.browser_helpers import app_for, selector
    app, client, worker, _ = app_for(connection_settings)
    monkeypatch.setattr('doneproof.adapters.browser.importlib.util.find_spec', lambda _: True)
    task = f'Verify browser check "release-7" at revision "{selector()["revision"]}" matches'
    session = client.post('/v1/assurance/sessions', headers=A, json={'task': task}).json()
    assert session['state'] == 'READY_FOR_EXECUTION'
    url = '/v1/assurance/sessions/' + session['id']
    job = client.post(url + '/verify', headers=A, json={}).json()['current_job_id']
    asyncio.run(worker.run_until_terminal('tenant-a', job))
    result = client.get(url, headers=A).json()
    assert result['receipt']['schema_version'] == '1.2'
    assert result['evidence'][0]['assurance_level'] == 'lower_than_authoritative_api'
    assert result['evidence'][0]['evidence_class'] == 'browser_ui'
    assert result['evidence'][0]['provenance']['executor_supplied'] is False
    receipt = VerificationReceipt.model_validate(result['receipt'])
    assert base64.b64decode(result['signed_payload_b64']) == ReceiptSigner._payload(receipt)
