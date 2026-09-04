from dataclasses import replace

from fastapi.testclient import TestClient

from doneproof.app import create_app


def sample_contract():
    return {"contract":{"task":"Create and assign issue","postconditions":[
        {"id":"p1","description":"created","provider":"mock","selector":{"state":{"created":True}},"predicate":{"op":"eq","path":"created","expected":True},"required":True},
        {"id":"p2","description":"assigned","provider":"mock","selector":{"state":{"assignees":[]}},"predicate":{"op":"contains","path":"assignees","expected":"alice"},"required":True},
    ]}}


def test_health_and_customer_surfaces(settings):
    client=TestClient(create_app(settings))
    assert client.get('/health').json()['ok'] is True
    assert 'Agents act' in client.get('/').text
    assert 'Outcome assurance' in client.get('/console').text


def test_verify_receipt_integrity_and_certificate(settings):
    client=TestClient(create_app(settings))
    r=client.post('/v1/verify',json=sample_contract())
    assert r.status_code == 200 and r.json()['verdict']=='PARTIAL'
    rid=r.json()['receipt_id']
    assert client.get(f'/v1/receipts/{rid}/integrity').json()['valid'] is True
    cert=client.get(f'/v1/receipts/{rid}/certificate')
    assert cert.status_code == 200 and rid in cert.text


def test_idempotency_returns_same_receipt(settings):
    client=TestClient(create_app(settings))
    h={'Idempotency-Key':'run-123'}
    a=client.post('/v1/verify',json=sample_contract(),headers=h)
    b=client.post('/v1/verify',json=sample_contract(),headers=h)
    assert a.json()['receipt_id']==b.json()['receipt_id']
    assert client.get('/v1/overview').json()['total']==1


def test_idempotency_conflict(settings):
    client=TestClient(create_app(settings))
    h={'Idempotency-Key':'same'}
    assert client.post('/v1/verify',json=sample_contract(),headers=h).status_code==200
    other=sample_contract(); other['contract']['task']='Different task'
    assert client.post('/v1/verify',json=other,headers=h).status_code==409


def test_tenant_receipt_isolation(auth_settings):
    app=create_app(auth_settings); client=TestClient(app)
    assert client.post('/v1/verify',json=sample_contract(),headers={'X-DoneProof-Key':'key-a'}).status_code==200
    assert len(client.get('/v1/receipts',headers={'X-DoneProof-Key':'key-a'}).json())==1
    assert len(client.get('/v1/receipts',headers={'X-DoneProof-Key':'key-b'}).json())==0
    assert client.get('/v1/receipts').status_code==401


def test_production_readiness_flags_missing_controls(settings):
    prod=replace(settings,env='production',api_keys={},signing_seed_b64=None,legacy_receipt_key='dev-only-change-me')
    body=TestClient(create_app(prod)).get('/ready').json()
    assert body['ready'] is False
    assert len(body['warnings'])==2


def test_demo_is_not_customer_visible_by_default(settings):
    prodish=replace(settings,enable_demo=False)
    assert TestClient(create_app(prodish)).post('/v1/demo/verify').status_code==404


def test_registered_run_gets_server_time_and_registered_assurance(settings):
    client=TestClient(create_app(settings))
    payload=sample_contract()
    payload['contract']['task_started_at']='2000-01-01T00:00:00Z'
    reg=client.post('/v1/runs',json=payload)
    assert reg.status_code==200
    assert not reg.json()['task_started_at'].startswith('2000-01-01')
    rid=reg.json()['id']
    verified=client.post(f'/v1/runs/{rid}/verify')
    assert verified.status_code==200
    assert verified.json()['assurance_level']=='registered'


def test_submitted_verify_is_marked_lower_assurance(settings):
    client=TestClient(create_app(settings))
    r=client.post('/v1/verify',json=sample_contract())
    assert r.json()['assurance_level']=='submitted'


def test_signing_key_is_public_even_when_workspace_auth_is_enabled(auth_settings):
    client=TestClient(create_app(auth_settings))
    r=client.get('/v1/signing-key')
    assert r.status_code==200
    assert r.json()['algorithm']=='Ed25519'


def test_request_body_limit_is_enforced(settings):
    tiny=replace(settings,max_body_bytes=80)
    client=TestClient(create_app(tiny))
    r=client.post('/v1/verify',content=b'{' + b'"x":"' + b'a'*200 + b'"}',headers={'content-type':'application/json'})
    assert r.status_code==413
