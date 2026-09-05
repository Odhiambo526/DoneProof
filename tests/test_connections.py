from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from doneproof.adapters.base import ObservationContext
from doneproof.app import create_app
from doneproof.connection_providers import GMAIL_SCOPE
from doneproof.connections import ConnectionService, ManagedAdapter
from doneproof.store import Store
from tests.connection_helpers import ACCESS, ADMIN_A, ADMIN_B, REFRESH, begin, body, finish, run, seed

CTX = ObservationContext("tenant-a", "contract1", "2026-01-01T00:00:00+00:00")
SELECTOR = {"message_id": "msg1"}


def client_for(app):
    return TestClient(app, base_url="https://testserver")


def test_management_requires_separate_admin_and_scopes_every_lookup(connection_app):
    app, _ = connection_app
    client = client_for(app)
    row = seed(app.state.connections)
    path = "/v1/connections/" + row["id"]
    for headers in ({}, {"X-DoneProof-Key": "key-a"}):
        assert client.get("/v1/connections", headers=headers).status_code == 401
        assert client.post("/v1/connections/gmail/authorize", headers=headers).status_code == 401
    assert client.get("/v1/connections", headers=ADMIN_B).json()["connections"] == []
    for suffix in ("", "/health", "/disconnect", "/rotate-key"):
        response = client.get(path, headers=ADMIN_B) if not suffix else client.post(path + suffix, headers=ADMIN_B)
        assert response.status_code == 404
    assert client.post("/v1/connections/gmail/authorize",
                       headers={**ADMIN_A, "Origin": "https://unrelated.example"}).status_code == 403
    assert client.get("/v1/overview", headers=ADMIN_A).status_code == 401
    assert client.get("/v1/connections", headers=ADMIN_A).status_code == 200


@pytest.mark.parametrize("provider", ["gmail", "github"])
def test_oauth_authorization_callback_and_public_projection(connection_app, provider):
    app, stub = connection_app
    client = client_for(app)
    query, started = begin(client, provider)
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    assert query["redirect_uri"] == [f"https://testserver/v1/connections/oauth/{provider}/callback"]
    cookie = started.headers["set-cookie"]
    assert all(value in cookie for value in ("HttpOnly", "Secure", "SameSite=lax", "__Host-"))
    if provider == "gmail":
        assert query["scope"] == [GMAIL_SCOPE] and query["access_type"] == ["offline"]
    else:
        assert "scope" not in query
    response = finish(client, query, provider)
    assert response.status_code == 303 and response.headers["location"] == "/connections#connected"
    row = app.state.connections.db.get("tenant-a", provider=provider)
    assert row["state"] == "connected"
    assert ACCESS not in row["credential_ciphertext"] and REFRESH not in row["credential_ciphertext"]
    public = client.get("/v1/connections", headers=ADMIN_A)
    assert public.json()["connections"][0]["account_label"]
    assert all(secret not in public.text for secret in (ACCESS, REFRESH, "ciphertext", "test-key", "test-code-sentinel"))
    assert finish(client, query, provider).headers["location"].endswith("authorization-failed")
    exchanges = [r for r in stub.requests if r.url.path in {"/token", "/login/oauth/access_token"}]
    assert len(exchanges) == 1
    assert body(exchanges[0])["code_verifier"]
    assert "test-code-sentinel" not in str(exchanges[0].url)
    audit = json.dumps(app.state.store.list_audit("tenant-a", 100))
    assert all(value not in audit for value in (ACCESS, REFRESH, query["state"][0], "test-code-sentinel"))


def test_callback_requires_original_browser_and_ignores_tenant_in_query(connection_app):
    app, _ = connection_app
    client = client_for(app)
    query, _ = begin(client)
    stranger = client_for(app)
    assert finish(stranger, query).headers["location"].endswith("authorization-failed")
    result = finish(client, query, tenant_id="tenant-b", redirect_uri="https://unrelated.example")
    assert result.headers["location"] == "/connections#connected"
    assert not app.state.connections.db.list("tenant-b")


def test_denied_and_duplicate_callback_fields_are_safe(connection_app):
    app, stub = connection_app
    client = client_for(app)
    query, _ = begin(client)
    response = finish(client, query, error="provider-error-with-test-secret")
    assert response.headers["location"].endswith("authorization-failed")
    assert "provider-error" not in response.text
    assert not stub.requests
    query, _ = begin(client)
    path = "/v1/connections/oauth/gmail/callback"
    response = client.get(path, params=[("state", query["state"][0]), ("state", "duplicate"),
                                       ("code", "test-code-sentinel")], follow_redirects=False)
    assert response.headers["location"].endswith("authorization-failed")
    assert not stub.requests


def test_disconnect_invalidates_pending_authorization(connection_app):
    app, stub = connection_app
    client = client_for(app)
    query, _ = begin(client)
    row = app.state.connections.db.get("tenant-a", provider="gmail")
    assert client.post(f"/v1/connections/{row['id']}/disconnect", headers=ADMIN_A).json()["state"] == "disabled"
    assert finish(client, query).headers["location"].endswith("authorization-failed")
    assert not stub.requests


def test_refresh_rotation_persists_atomically_and_health_recovers(connection_app):
    app, stub = connection_app
    service = app.state.connections
    row = seed(service, expires_in=-1)
    before = service.db.public(row)
    assert before["state"] == "expired"
    resolved = run(service.usable("tenant-a", "gmail"))
    assert resolved[1]["access_token"] == stub.rotated
    assert resolved[1]["refresh_token"] == "test-next-refresh"
    assert stub.refresh_calls == 1 and resolved[0]["expires_at"] > time.time()
    assert service.vault.decrypt(service.db.get("tenant-a", provider="gmail"))["refresh_token"] == "test-next-refresh"
    assert run(service.usable("tenant-a", "gmail")) and stub.refresh_calls == 1
    stub.status = 503
    assert run(service.usable("tenant-a", "gmail", check_health=True)) is None
    assert service.db.get("tenant-a", provider="gmail")["state"] == "error"
    stub.status = 200
    assert run(service.usable("tenant-a", "gmail", check_health=True))[0]["state"] == "connected"


@pytest.mark.parametrize("mode", ["no_refresh", "refresh_expired", "invalid_grant", "crashed_refresh", "revoked"])
def test_unusable_credentials_fail_closed(connection_app, mode):
    app, stub = connection_app
    service = app.state.connections
    row = seed(service, expires_in=-1 if mode != "revoked" else 3600)
    if mode == "no_refresh":
        credentials = service.vault.decrypt(row)
        credentials.pop("refresh_token")
        service.db.update(row, credential_ciphertext=service.vault.encrypt(row, credentials))
    elif mode == "refresh_expired":
        service.db.update(row, refresh_expires_at=int(time.time()) - 1)
    elif mode == "invalid_grant":
        stub.status = 400
    elif mode == "crashed_refresh":
        db = service.db
        with db.transaction() as con:
            db.execute(con, "UPDATE connections SET lease_id='old',lease_until=1 WHERE tenant_id=? AND id=?",
                       ("tenant-a", row["id"]))
    elif mode == "revoked":
        stub.message_status = 401
    observation = run(ManagedAdapter(service, "gmail").observe(SELECTOR, CTX))
    assert observation.indeterminate and observation.state is None
    assert service.db.get("tenant-a", provider="gmail")["state"] in {"expired", "reconnect_required"}
    assert service.capability("tenant-a", "gmail") == "configuration_required"


def test_concurrent_refresh_only_one_provider_exchange(connection_app):
    app, stub = connection_app
    service = app.state.connections
    seed(service, expires_in=-1)

    async def scenario():
        async def pause():
            await asyncio.sleep(0.2)
        stub.pause_refresh = pause
        return await asyncio.gather(service.usable("tenant-a", "gmail"), service.usable("tenant-a", "gmail"))
    results = run(scenario())
    assert all(results) and stub.refresh_calls == 1


def test_disconnect_during_refresh_cannot_restore_connection(connection_app):
    app, stub = connection_app
    service = app.state.connections
    row = seed(service, expires_in=-1)

    async def scenario():
        async def disconnect():
            await service.disconnect("tenant-a", row["id"])
        stub.pause_refresh = disconnect
        return await service.usable("tenant-a", "gmail")
    assert run(scenario()) is None
    current = service.db.get("tenant-a", provider="gmail")
    assert current["state"] == "disabled" and current["credential_ciphertext"] is None


def test_disconnect_during_observation_discards_evidence(connection_app):
    app, stub = connection_app
    service = app.state.connections
    row = seed(service)

    async def scenario():
        async def disconnect():
            await service.disconnect("tenant-a", row["id"])
        stub.pause_observe = disconnect
        return await ManagedAdapter(service, "gmail").observe(SELECTOR, CTX)
    assert run(scenario()).indeterminate


def test_disconnect_revokes_and_retries_without_credential_reuse(connection_app):
    app, stub = connection_app
    service = app.state.connections
    row = seed(service)
    stub.revoke_status = 503
    result = run(service.disconnect("tenant-a", row["id"]))
    assert result["state"] == "disabled" and result["revocation_pending"] == 1
    assert result["credential_ciphertext"]
    assert run(service.usable("tenant-a", "gmail")) is None
    assert client_for(app).post("/v1/connections/gmail/authorize", headers=ADMIN_A).status_code == 409
    stub.revoke_status = 200
    result = run(service.disconnect("tenant-a", row["id"]))
    assert result["credential_ciphertext"] is None and result["revocation_pending"] == 0
    assert body(stub.requests[-1])["token"] == [REFRESH]
    assert REFRESH not in str(stub.requests[-1].url)


def test_provider_health_identity_and_permissions_are_authoritative(connection_app):
    app, stub = connection_app
    client = client_for(app)
    stub.installations["installations"][0]["permissions"]["issues"] = "write"
    query, _ = begin(client, "github")
    assert finish(client, query, "github").headers["location"].endswith("authorization-failed")
    assert app.state.connections.capability("tenant-a", "github") == "configuration_required"
    stub.token_data["scope"] = "unrelated.scope"
    query, _ = begin(client)
    assert finish(client, query).headers["location"].endswith("authorization-failed")
    assert app.state.connections.capability("tenant-a", "gmail") == "configuration_required"


def test_no_runtime_global_token_fallback(connection_app, monkeypatch):
    app, stub = connection_app
    service = app.state.connections
    monkeypatch.setenv("GITHUB_TOKEN", "global-must-not-be-sent")
    result = run(ManagedAdapter(service, "github").observe(
        {"repo": "example/project", "kind": "issue", "number": 1}, CTX))
    assert not result.indeterminate
    assert all("authorization" not in request.headers for request in stub.requests)
    assert run(ManagedAdapter(service, "gmail").observe(SELECTOR, CTX)).indeterminate
    seed(service)
    other = replace(CTX, tenant_id="tenant-b")
    assert run(ManagedAdapter(service, "gmail").observe(SELECTOR, other)).indeterminate


def test_transition_account_binding_survives_same_account_refresh_but_rejects_switch(connection_app):
    app, stub = connection_app
    service = app.state.connections
    row = seed(service)
    capture = replace(CTX, require_connection_binding=True, capture_connection_binding=True, condition_id="p1")
    verify = replace(capture, capture_connection_binding=False)
    assert not run(ManagedAdapter(service, "gmail").observe(SELECTOR, capture)).indeterminate
    assert not run(ManagedAdapter(service, "gmail").observe(SELECTOR, verify)).indeterminate
    row = service.db.get("tenant-a", provider="gmail")
    service.db.update(row, account_id="another@example.test")
    assert run(ManagedAdapter(service, "gmail").observe(SELECTOR, verify)).indeterminate
    assert run(ManagedAdapter(service, "gmail").observe(SELECTOR,
        replace(verify, contract_id="pre-migration-contract"))).indeterminate


def test_capabilities_reflect_connection_state_without_changing_existing_shape(connection_app):
    app, _ = connection_app
    service = app.state.connections
    client = client_for(app)
    row = seed(service)
    def capability():
        data = client.get("/v1/capabilities", headers={"X-DoneProof-Key": "key-a"}).json()
        return next(item for item in data["providers"] if item["provider"] == "gmail")["status"]
    assert capability() == "available"
    row = service.db.update(row, expires_at=1)
    assert capability() == "configuration_required"
    service.db.disable(row)
    assert capability() == "disabled"


def test_connection_ui_hardening_and_console_navigation(connection_app):
    app, _ = connection_app
    client = client_for(app)
    assert 'href="/connections"' in client.get("/console").text
    page = client.get("/connections")
    assert page.status_code == 200 and "Connection Settings" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "script-src 'self';" in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "no-referrer"
    script = client.get("/connections.js").text
    assert "textContent" in script and "sessionStorage" not in script and "innerHTML" not in script


def test_legacy_import_is_encrypted_scoped_and_never_resurrects(connection_settings):
    settings = replace(connection_settings, gmail_tokens={"tenant-a": "legacy-token-sentinel"})
    service = ConnectionService(Store(settings.storage_dsn), settings)
    row = service.db.get("tenant-a", provider="gmail")
    assert row["state"] == "error" and "legacy-token-sentinel" not in row["credential_ciphertext"]
    assert service.db.list("tenant-b") == []
    service.db.disable(row)
    restarted = ConnectionService(Store(settings.storage_dsn), settings)
    assert restarted.db.get("tenant-a", provider="gmail")["state"] == "disabled"
    with pytest.raises(RuntimeError, match="Global legacy"):
        ConnectionService(Store(settings.storage_dsn), replace(settings, github_token="global-token"))
    with pytest.raises(RuntimeError, match="encryption"):
        ConnectionService(Store(settings.storage_dsn), replace(settings, connection_encryption_keys={},
                                                               connection_active_key=None))


def test_invalid_config_and_admin_key_overlap_fail_closed(settings):
    with pytest.raises(RuntimeError, match="distinct"):
        create_app(replace(settings, api_keys={"same": "tenant"}, connection_admin_keys={"same": "tenant"}))
    for origin in ("http://example.test", "https://user:password@example.test", "https://example.test/?redirect=other"):
        with pytest.raises(RuntimeError, match="HTTPS origin"):
            create_app(replace(settings, connection_public_url=origin))
    with pytest.raises(RuntimeError, match="encryption key"):
        create_app(replace(settings, connection_encryption_keys={"key": base64.b64encode(b"short").decode()},
                           connection_active_key="key"))



def test_registered_gmail_transition_and_receipt_integrity(connection_app):
    from doneproof.domain import VerificationReceipt
    from doneproof.signing import ReceiptSigner
    app, stub = connection_app
    client = client_for(app)
    seed(app.state.connections)
    key = {"X-DoneProof-Key": "key-a"}
    contract = {"task": "Send report", "postconditions": [{
        "id": "sent", "description": "Report was sent", "provider": "gmail",
        "selector": SELECTOR, "predicate": {"op": "eq", "path": "location", "expected": "sent"},
        "require_change": True}]}
    stub.labels = ["DRAFT"]
    registered = client.post("/v1/runs", headers=key, json={"contract": contract})
    assert registered.status_code == 200
    run_id = registered.json()["id"]
    stub.labels = ["SENT"]
    response = client.post(f"/v1/runs/{run_id}/verify", headers=key)
    assert response.status_code == 200 and response.json()["verdict"] == "VERIFIED"
    receipt = VerificationReceipt.model_validate(response.json())
    assert ReceiptSigner.verify_trusted(receipt, app.state.signer.public_key_b64)
    assert all(value not in response.text for value in (ACCESS, REFRESH, "credential_ciphertext", "connection_admin_keys"))
    assert client.post(f"/v1/runs/{run_id}/verify", headers={"X-DoneProof-Key": "key-b"}).status_code == 404
    row = app.state.connections.db.get("tenant-a", provider="gmail")
    app.state.connections.db.update(row, account_id="changed@example.test")
    assert client.post(f"/v1/runs/{run_id}/verify", headers=key).json()["verdict"] == "UNKNOWN"
    # Existing receipts remain verifiable after disconnect or account changes.
    assert ReceiptSigner.verify_trusted(receipt, app.state.signer.public_key_b64)


@pytest.mark.parametrize("path", [
    "/v1/connections/oauth/gmail/callback", "/v1/connections/oauth/gmail/callback/",
    "/v1/connections/oauth/unrecognized/callback",
])
def test_callback_query_is_removed_from_access_log_scope(path):
    from uvicorn.protocols.utils import get_path_with_query_string

    from doneproof.connection_api import CallbackQueryPrivacy
    scope = {"type": "http", "path": path,
             "raw_path": path.encode(),
             "query_string": b"state=test-state-sentinel&code=test-code-sentinel"}
    async def downstream(scope, receive, send):
        assert scope["doneproof.oauth"]["code"] == "test-code-sentinel"
        assert "sentinel" not in get_path_with_query_string(scope)
        assert scope["query_string"] == b""
    run(CallbackQueryPrivacy(downstream)(scope, None, None))


def test_callback_cleanup_failure_is_encrypted_and_retryable(connection_app):
    app, stub = connection_app
    client = client_for(app)
    stub.token_data["scope"] = "unrelated.scope"
    stub.revoke_status = 503
    query, _ = begin(client)
    assert finish(client, query).headers["location"].endswith("authorization-failed")
    service = app.state.connections
    row = service.db.get("tenant-a", provider="gmail")
    assert row["state"] == "disabled" and row["revocation_pending"]
    pending = service.db.revocations(row)
    assert len(pending) == 1 and ACCESS not in pending[0]["credential_ciphertext"]
    assert client.post("/v1/connections/gmail/authorize", headers=ADMIN_A).status_code == 409
    stub.revoke_status = 200
    run(service.disconnect("tenant-a", row["id"]))
    current = service.db.get("tenant-a", provider="gmail")
    assert not service.db.revocations(current)
    assert not current["revocation_pending"]


def test_explicit_external_revocation_never_becomes_evidence(connection_app):
    app, _ = connection_app
    client = client_for(app)
    service = app.state.connections
    row = seed(service, provider="github", kind="legacy")
    endpoint = f"/v1/connections/{row['id']}/confirm-external-revocation"
    assert client.post(endpoint, headers=ADMIN_A).status_code == 409
    row = run(service.disconnect("tenant-a", row["id"]))
    assert row["revocation_pending"] and row["state"] == "disabled"
    assert client.post(endpoint, headers=ADMIN_B).status_code == 404
    result = client.post(endpoint, headers=ADMIN_A)
    assert result.json()["state"] == "disabled" and not result.json()["revocation_pending"]
    assert service.db.get("tenant-a", provider="github")["credential_ciphertext"] is None
    assert service.capability("tenant-a", "github") == "disabled"


@pytest.mark.parametrize("payload", [
    {}, {"access_token": "test-access", "expires_in": -1},
    {"access_token": "test-access", "expires_in": True},
    {"access_token": "test-access", "scope": []},
    {"access_token": "test-access", "token_type": "MAC"},
])
def test_malformed_token_response_is_fail_closed(connection_app, payload):
    app, stub = connection_app
    stub.token_data = payload
    client = client_for(app)
    query, _ = begin(client)
    assert finish(client, query).headers["location"].endswith("authorization-failed")
    assert app.state.connections.capability("tenant-a", "gmail") != "available"


def test_token_exchange_redirect_is_not_followed(connection_app):
    import httpx
    app, _ = connection_app
    requests = []
    def provider(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://unrelated.example/token"})
    app.state.connections.providers.transport = httpx.MockTransport(provider)
    client = client_for(app)
    query, _ = begin(client)
    assert finish(client, query).headers["location"].endswith("authorization-failed")
    assert len(requests) == 1
    assert requests[0].url.host == "oauth2.googleapis.com"


def test_key_rotation_api_reencrypts_without_exposing_plaintext(connection_app):
    app, _ = connection_app
    service = app.state.connections
    row = seed(service)
    original = row["credential_ciphertext"]
    response = client_for(app).post(f"/v1/connections/{row['id']}/rotate-key", headers=ADMIN_A)
    assert response.status_code == 200 and ACCESS not in response.text
    row = service.db.get("tenant-a", provider="gmail")
    assert row["credential_ciphertext"] != original
    assert service.vault.decrypt(row)["access_token"] == ACCESS
