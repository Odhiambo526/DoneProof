from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from doneproof.app import create_app
from doneproof.config import WebhookSource
from tests.connection_helpers import seed

A = {"X-DoneProof-Key": "key-a"}
B = {"X-DoneProof-Key": "key-b"}


def compile_task(client, task, headers=A, **extra):
    response = client.post("/v2/contracts/compile", headers=headers, json={"task": task, **extra})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_compilation_uses_managed_tenant_connection_and_stores_only_valid_contract(connection_app):
    app, stub = connection_app
    seed(app.state.connections)
    with TestClient(app) as client:
        assert client.post("/v2/contracts/compile", json={"task": "Check Gmail message msg1 is sent"}).status_code == 401
        good = compile_task(client, "Check Gmail message msg1 is sent")
        assert good["status"] == "valid_contract"
        bad = compile_task(client, "Check Gmail message msg1 is sent", B)
        assert bad["status"] == "unverifiable_outcome"
        assert bad["contract"] is None
        ident = good["contract"]["id"]
        assert app.state.store.get_contract("tenant-a", ident) is not None
        assert app.state.store.get_contract("tenant-b", ident) is None
        assert len([r for r in stub.requests if "/messages/" in r.url.path]) == 1
        assert "test-access-sentinel" not in json.dumps(good)
        assert "contract_quality" not in app.state.store.get_contract("tenant-a", ident).model_dump()


def test_expired_refreshable_connection_is_recovered_for_compilation(connection_app):
    app, stub = connection_app
    seed(app.state.connections, expires_in=-10)
    with TestClient(app) as client:
        result = compile_task(client, "Check Gmail message msg1 is sent")
    assert result["status"] == "valid_contract"
    assert stub.refresh_calls == 1


def test_disabled_connection_remains_fail_closed(connection_app):
    app, stub = connection_app
    row = seed(app.state.connections)
    app.state.connections.db.disable(row)
    with TestClient(app) as client:
        result = compile_task(client, "Check Gmail message msg1 is sent")
    assert result["contract"] is None
    assert not stub.requests


def test_webhook_capability_cannot_cross_tenants(connection_settings):
    app = create_app(replace(connection_settings,
        webhook_sources={"erp": WebhookSource(tenant_id="tenant-a", secret="hook-secret-sentinel")}))
    task = 'Wait for webhook "refund.completed" from "erp" for object "order-9"'
    with TestClient(app) as client:
        a = compile_task(client, task)
        b = compile_task(client, task, B, context={"tenant_id": "tenant-a", "executor_claim": "refund complete"})
    assert a["status"] == "valid_contract"
    assert a["selector_checks"][0]["status"] == "deferred"
    assert b["contract"] is None
    assert "hook-secret-sentinel" not in json.dumps(a)


def test_preflight_is_not_a_baseline_or_receipt(connection_app):
    app, _ = connection_app
    seed(app.state.connections, "github")
    with TestClient(app) as client:
        result = compile_task(client, "Close issue #1 in example/project")
        assert result["status"] == "valid_contract"
        submitted = client.post("/v1/verify", headers=A, json={"contract": result["contract"]})
    assert submitted.status_code == 200
    assert submitted.json()["verdict"] == "UNKNOWN"
    assert submitted.json()["results"][0]["transition_required"] is True
    assert "contract_quality" not in submitted.json()


def test_v1_success_shape_and_structured_v2_failures(connection_app):
    app, _ = connection_app
    seed(app.state.connections, "github")
    with TestClient(app) as client:
        response = client.post("/v1/contracts/compile", headers=A, json={"task": "Close issue #1 in example/project"})
        assert response.status_code == 200
        assert "postconditions" in response.json() and "contract_quality" not in response.json()
        response = client.post("/v1/contracts/compile", headers=A, json={"task": "Post a Slack message"})
        assert response.status_code == 503
        result = compile_task(client, "Post a Slack message")
        assert result["status"] == "unsupported_provider"
        assert result["clarification_requirements"][0]["code"] == "unsupported_provider"
        result = compile_task(client, "Close issue")
        assert result["status"] == "missing_identifier"
        assert result["clarification_requirements"][0]["fields"]


def test_sensitive_context_never_reaches_transport_or_storage(connection_app, caplog):
    app, stub = connection_app
    with TestClient(app) as client:
        result = compile_task(client, "Close issue #1 in example/project", context={"token": "secret-sentinel"})
    assert result["contract"] is None
    assert result["clarification_requirements"][0]["code"] == "sensitive_input"
    assert "secret-sentinel" not in json.dumps(result) + caplog.text
    assert not stub.requests


@pytest.mark.parametrize("count", [0, 1, 2])
def test_existing_discovery_pins_only_authoritative_unique_id(connection_app, count):
    app, stub = connection_app
    seed(app.state.connections, "github")
    async def provider(request):
        if request.url.path.endswith("/issues"):
            return httpx.Response(200, json=[{"number": i + 1, "title": "Fix checkout", "user": {"login": "maya"},
                "created_at": "2026-01-01T00:00:00Z"} for i in range(count)])
        if request.url.path.endswith("/issues/1"):
            return httpx.Response(200, json={"number": 1, "title": "Fix checkout", "state": "open"})
        return await stub(request)
    app.state.connections.providers.transport = httpx.MockTransport(provider)
    with TestClient(app) as client:
        result = compile_task(client, 'Close issue in example/project titled "Fix checkout"')
    if count == 1:
        assert result["status"] == "valid_contract"
        assert result["contract"]["postconditions"][0]["selector"] == {"repo": "example/project", "kind": "issue", "number": 1}
        assert result["contract"]["postconditions"][0]["require_change"] is True
    else:
        assert result["contract"] is None
        assert result["status"] == ("missing_identifier" if count == 0 else "ambiguous_resource")


def test_alias_selectors_cannot_hide_contradictory_predicates(connection_app):
    app, stub = connection_app
    seed(app.state.connections, "github")
    async def provider(request):
        if request.url.path.endswith("/issues"):
            return httpx.Response(200, json=[{"number": 1, "title": "Fix checkout", "created_at": "2026-01-01T00:00:00Z"}])
        if request.url.path.endswith("/issues/1"):
            return httpx.Response(200, json={"number": 1, "title": "Fix checkout", "state": "open"})
        return await stub(request)
    app.state.connections.providers.transport = httpx.MockTransport(provider)
    with TestClient(app) as client:
        result = compile_task(client, 'Close issue in example/project titled "Fix checkout"; Reopen issue #1 in example/project')
    assert result["contract"] is None
    assert "contradictory_predicates" in [x["code"] for x in result["clarification_requirements"]]


def test_v2_rate_limit_uses_tenant_key(connection_settings):
    app = create_app(replace(connection_settings, requests_per_minute=1))
    with TestClient(app) as client:
        assert client.get("/v2/contracts/capabilities", headers=A).status_code == 200
        limited = client.get("/v2/contracts/capabilities", headers=A)
        assert limited.status_code == 429 and limited.headers["retry-after"]
        assert client.get("/v2/contracts/capabilities", headers=B).status_code == 200


def test_v2_validation_error_does_not_echo_credentials(connection_app):
    app, _ = connection_app
    with TestClient(app) as client:
        response = client.post("/v2/contracts/compile", headers=A,
                               json={"task": {"access_token": "validation-secret-sentinel"}})
    assert response.status_code == 422
    assert "validation-secret-sentinel" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_connection_revoked_during_preflight_cannot_produce_contract(connection_app):
    app, stub = connection_app
    row = seed(app.state.connections)
    async def revoke():
        app.state.connections.db.disable(row)
    stub.pause_observe = revoke
    with TestClient(app) as client:
        result = compile_task(client, "Check Gmail message msg1 is sent")
    assert result["contract"] is None
    assert result["selector_checks"][0]["status"] == "unavailable"
