from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from doneproof.app import create_app
from doneproof.job_callbacks import CallbackRegistry
from doneproof.worker import VerificationWorker
from tests.test_jobs import A, B, Provider, payload, submit, update

SECRET = "callback-secret-sentinel-32-bytes-long"
CONFIG = {"tenant-a": {"audit": {"url": "https://receiver.example.org/doneproof", "secret": SECRET}}}


@pytest.fixture
def callbacks(connection_settings):
    app = create_app(replace(connection_settings, job_callbacks=CONFIG), {"github": Provider()})
    client = TestClient(app)
    requests = []
    def accept(request):
        requests.append(request)
        return httpx.Response(204)
    worker = VerificationWorker(app.state.store, app.state.engine, app.state.job_callbacks,
                                callback_transport=httpx.MockTransport(accept))
    return app, client, worker, requests


@pytest.mark.parametrize("url", ["http://receiver.example.org/path", "https://localhost/path", "https://127.0.0.1/path",
    "https://169.254.169.254/path", "https://[::1]/path", "https://user:password@example.org/path",
    "https://receiver.example.org/path?token=secret", "https://receiver.example.org/path#fragment"])
def test_callbacks_require_fixed_public_https_configuration(url):
    with pytest.raises(RuntimeError):
        CallbackRegistry({"tenant-a": {"audit": {"url": url, "secret": SECRET}}})


def test_callback_scope_and_arbitrary_url_input(callbacks):
    _, client, _, _ = callbacks
    body = {**payload(), "callback_id": "audit"}
    assert client.post("/v1/jobs", json=body, headers=B).status_code == 422
    assert client.post("/v1/jobs", json={**payload(), "callback_url": "https://other.example/path"}, headers=A).status_code == 422
    assert client.post("/v1/jobs", json=body, headers=A).status_code == 202


def test_outbox_atomic_signed_delivery_and_duplicate_publication(callbacks):
    app, client, worker, requests = callbacks
    identifier = submit(client, {**payload(), "callback_id": "audit"})
    result = asyncio.run(worker.run_until_terminal("tenant-a", identifier))
    assert not requests
    assert asyncio.run(worker.callback_tick())
    assert not asyncio.run(worker.callback_tick())
    request = requests[0]
    expected = hmac.new(SECRET.encode(), request.headers["X-DoneProof-Timestamp"].encode() + b"." + request.content, hashlib.sha256).hexdigest()
    assert request.headers["X-DoneProof-Signature"] == "sha256=" + expected
    data = json.loads(request.content)
    assert data["receipt_id"] == result["receipt_id"] and data["job_id"] == identifier
    assert data["event_id"] == request.headers["X-DoneProof-Event"]
    public = client.get(f"/v1/jobs/{identifier}", headers=A)
    assert public.json()["callback"]["state"] == "DELIVERED"
    assert SECRET not in request.content.decode() + public.text
    assert "provider-secret-sentinel" not in request.content.decode()
    assert app.state.store.get_receipt("tenant-a", result["receipt_id"])


def test_callback_retry_after_and_recovery_reuse_event_id(callbacks):
    _, client, worker, requests = callbacks
    identifier = submit(client, {**payload(), "callback_id": "audit"})
    asyncio.run(worker.run_until_terminal("tenant-a", identifier))
    def limited(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "600"})
    worker.callback_transport = httpx.MockTransport(limited)
    asyncio.run(worker.callback_tick())
    with worker.db.transaction() as con:
        row = worker.db._row(worker.db.execute(con, "SELECT * FROM verification_callback_outbox WHERE job_id=?", (identifier,)))
        assert row["next_attempt_at"] >= worker.db.now(con) + 599
    assert not asyncio.run(worker.callback_tick())
    update(worker.db, "UPDATE verification_callback_outbox SET next_attempt_at=0 WHERE job_id=?", (identifier,))
    claimed = worker.db.claim_callback()
    # A crash after remote acceptance is ambiguous: retry with the same event ID, at most six attempts.
    update(worker.db, "UPDATE verification_callback_outbox SET lease_until=0 WHERE job_id=?", (identifier,))
    recovered = worker.db.claim_callback()
    assert recovered["event_id"] == claimed["event_id"] and recovered["attempts"] == 3
    worker.db.finish_callback(claimed, "DELIVERED")
    worker.db.finish_callback(recovered, "DEAD", "callback_rejected")
    assert client.get(f"/v1/jobs/{identifier}", headers=A).json()["callback"]["state"] == "DEAD"


@pytest.mark.parametrize("code", [301, 400, 401, 403, 404])
def test_callback_semantic_failures_and_redirects_are_not_retried(callbacks, code):
    _, client, worker, requests = callbacks
    identifier = submit(client, {**payload(), "callback_id": "audit"})
    asyncio.run(worker.run_until_terminal("tenant-a", identifier))
    def reject(request):
        requests.append(request)
        return httpx.Response(code, headers={"Location": "https://other.example/path"})
    worker.callback_transport = httpx.MockTransport(reject)
    asyncio.run(worker.callback_tick())
    assert len(requests) == 1
    assert client.get(f"/v1/jobs/{identifier}", headers=A).json()["callback"]["state"] == "DEAD"


def test_configuration_changes_do_not_retarget_queued_callback(callbacks):
    app, client, worker, requests = callbacks
    body = {**payload(), "callback_id": "audit"}
    identifier = submit(client, body)
    worker.db.cancel("tenant-a", identifier)
    worker.callbacks = CallbackRegistry({"tenant-a": {"audit": {"url": "https://new.example.org/path", "secret": SECRET}}})
    asyncio.run(worker.callback_tick())
    assert not requests
    assert client.get(f"/v1/jobs/{identifier}", headers=A).json()["callback"]["error_code"] == "callback_configuration_changed"
    app.state.job_callbacks = CallbackRegistry({})
    assert client.post("/v1/jobs", json=body, headers=A).json()["id"] == identifier
