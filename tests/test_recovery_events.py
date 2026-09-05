from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from doneproof.app import create_app
from doneproof.config import WebhookSource
from doneproof.recovery_store import RecoveryStore
from doneproof.store import Store
from doneproof.worker import VerificationWorker
from tests.test_jobs import A, B, run, update
from tests.test_recovery import again, original
from tests.test_webhook import sign


@pytest.fixture
def events(connection_settings):
    settings = replace(connection_settings, webhook_sources={"erp": WebhookSource("tenant-a", "event-test-secret"),
                                                             "other": WebhookSource("tenant-b", "other-secret")})
    app = create_app(settings)
    client = TestClient(app)
    body = {"contract": {"task": "Verify order delivery", "task_started_at": time.time() - 10,
        "postconditions": [{"id": "p1", "description": "The exact order was delivered", "provider": "webhook",
            "selector": {"source": "erp", "event_type": "order.updated", "object_id": "order-1"},
            "predicate": {"op": "eq", "path": "payload.status", "expected": "delivered"}}]}}
    root = original(client, body)
    policy = client.post(f"/v1/receipts/{root['receipt_id']}/recovery-policy", headers=A, json={"automatic": True})
    assert policy.status_code == 200, policy.text
    worker = VerificationWorker(app.state.store, app.state.engine, recovery=app.state.recovery)
    return app, client, worker, root


def emit(client, payload=None, *, source="erp", secret="event-test-secret", timestamp=None,
         event_type="order.updated", object_id="order-1", valid=True):
    raw = json.dumps(payload if payload is not None else {"status": "delivered"}, separators=(",", ":")).encode()
    ts = str(timestamp if timestamp is not None else int(time.time()) + 2)
    headers = {"Content-Type": "application/json", "X-DoneProof-Timestamp": ts,
        "X-DoneProof-Event": event_type, "X-DoneProof-Object-ID": object_id,
        "X-DoneProof-Signature": sign(secret, ts, event_type, object_id, raw) if valid else "sha256=invalid"}
    return client.post(f"/v1/webhooks/{source}", content=raw, headers=headers)


def queue(db):
    with db.transaction() as con:
        return [dict(r) for r in db.execute(con, "SELECT * FROM recovery_event_queue ORDER BY event_id").fetchall()]


def test_authenticated_event_resumes_after_restart_and_is_idempotent(events):
    app, client, _, root = events
    timestamp = int(time.time()) + 2
    response = emit(client, timestamp=timestamp)
    assert response.status_code == 200 and len(queue(app.state.recovery)) == 1
    assert emit(client, timestamp=timestamp).json()["duplicate"]
    restarted = Store(app.state.settings.storage_dsn)
    worker = VerificationWorker(restarted, app.state.engine, recovery=RecoveryStore(restarted))
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: RecoveryStore(restarted).dispatch_event(), range(3)))
    scheduled = queue(worker.recovery)
    assert len(scheduled) == 1 and scheduled[0]["state"] == "DONE"
    result = run(worker, scheduled[0]["job_id"])
    receipt = restarted.get_receipt("tenant-a", result["receipt_id"])
    assert receipt.verdict == "VERIFIED" and receipt.previous_receipt_id == root["receipt_id"]
    assert receipt.results[0].evidence.observed == "delivered"
    assert app.state.recovery.history("tenant-a", root["receipt_id"])["attempts_used"] == 1
    assert not asyncio.run(worker.recovery_tick())


@pytest.mark.parametrize("options", [{"source": "other", "secret": "other-secret"},
    {"object_id": "order-2"}, {"event_type": "other.updated"}, {"valid": False}])
def test_wrong_tenant_resource_or_signature_cannot_trigger_verification(events, options):
    app, client, worker, _ = events
    response = emit(client, **options)
    assert response.status_code == (401 if options.get("valid") is False else 200)
    assert queue(app.state.recovery) == [] and not asyncio.run(worker.recovery_tick())


def test_stale_evidence_is_ignored_and_disabled_subscription_stays_disabled(events):
    app, client, worker, root = events
    assert emit(client, timestamp=int(time.time()) - 5).status_code == 200
    asyncio.run(worker.recovery_tick())
    assert queue(app.state.recovery)[0]["reason"] == "evidence_not_newer_than_observation"
    response = client.post(f"/v1/receipts/{root['receipt_id']}/recovery-policy", headers=A, json={"automatic": False})
    assert response.status_code == 200
    assert emit(client).status_code == 200
    assert len(queue(app.state.recovery)) == 1
    assert app.state.recovery.history("tenant-a", root["receipt_id"])["attempts_used"] == 0


def test_guidance_cannot_be_credited_via_the_trusted_webhook_path(events):
    app, client, worker, root = events
    copied = {"status": "delivered", "remediation": root["remediation"]}
    assert emit(client, copied).status_code == 200
    assert queue(app.state.recovery) == []
    job = again(client, root["receipt_id"]).json()["id"]
    result = run(worker, job)
    receipt = app.state.store.get_receipt("tenant-a", result["receipt_id"])
    assert receipt.verdict == "UNKNOWN" and receipt.summary.passed == 0


def test_evidence_insert_and_trigger_enqueue_commit_atomically(events, monkeypatch):
    app, client, _, _ = events
    original_enqueue = RecoveryStore.enqueue_event
    def interrupt(*args):
        original_enqueue(*args)
        raise RuntimeError("simulated event ingestion interruption")
    with monkeypatch.context() as patch:
        patch.setattr(RecoveryStore, "enqueue_event", interrupt)
        with pytest.raises(RuntimeError):
            emit(client)
    with app.state.recovery.transaction() as con:
        assert app.state.recovery.execute(con, "SELECT COUNT(*) AS n FROM evidence_events").fetchone()["n"] == 0
    assert queue(app.state.recovery) == []
    assert emit(client).status_code == 200 and len(queue(app.state.recovery)) == 1


def test_event_is_not_lost_while_another_attempt_is_active(events):
    app, client, worker, root = events
    active = again(client, root["receipt_id"]).json()["id"]
    assert emit(client).status_code == 200
    asyncio.run(worker.recovery_tick())
    assert queue(app.state.recovery)[0]["state"] == "PENDING"
    client.post(f"/v1/jobs/{active}/cancel", headers=A)
    update(worker.db, "UPDATE recovery_event_queue SET next_at=0")
    asyncio.run(worker.recovery_tick())
    scheduled = queue(worker.db)[0]
    assert scheduled["state"] == "DONE" and scheduled["job_id"] != active
    result = run(worker, scheduled["job_id"])
    assert app.state.store.get_receipt("tenant-a", result["receipt_id"]).verdict == "VERIFIED"
    assert app.state.recovery.history("tenant-a", root["receipt_id"])["attempts_used"] == 2


def test_automatic_policy_rejects_arbitrary_urls_and_foreign_workspaces(events):
    _, client, _, root = events
    path = f"/v1/receipts/{root['receipt_id']}/recovery-policy"
    response = client.post(path, headers=A, json={"automatic": True, "url": "https://example.org/execute"})
    assert response.status_code == 422
    assert client.post(path, headers=B, json={"automatic": True}).status_code == 404


def test_automatic_reverification_stops_repeated_failures(events):
    app, client, worker, root = events
    timestamp = int(time.time()) + 2
    for i in range(3):
        assert emit(client, {"status": "pending"}, timestamp=timestamp + i).status_code == 200
        asyncio.run(worker.recovery_tick())
        current = app.state.recovery.history("tenant-a", root["receipt_id"])
        assert current["active_job_id"]
        run(worker, current["active_job_id"])
    history = app.state.recovery.history("tenant-a", root["receipt_id"])
    assert history["receipts"][-1]["recovery"]["repeated_failures"] == ["p1"]
    assert emit(client, {"status": "pending"}, timestamp=timestamp + 4).status_code == 200
    asyncio.run(worker.recovery_tick())
    assert app.state.recovery.history("tenant-a", root["receipt_id"])["attempts_used"] == 3
    assert any(item["reason"] == "repeated_or_oscillating_failure" for item in queue(app.state.recovery))
