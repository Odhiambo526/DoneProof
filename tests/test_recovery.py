from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from doneproof.adapters.base import ProviderAdapter, ProviderObservation
from doneproof.app import create_app
from doneproof.domain import ConditionStatus, VerificationReceipt
from doneproof.recovery_store import RecoveryStore
from doneproof.remediation import failure_patterns, remediation_for
from doneproof.signing import ReceiptSigner
from doneproof.worker import VerificationWorker
from tests.test_jobs import A, B, payload, run, submit, update


class StateProvider(ProviderAdapter):
    def __init__(self):
        self.state = {"ok": False}
        self.calls = []
        self.unknown = False

    async def observe(self, selector, context):
        self.calls.append((dict(selector), context))
        return ProviderObservation(self.state, indeterminate=self.unknown)


@pytest.fixture
def recovery_app(connection_settings):
    adapter = StateProvider()
    app = create_app(connection_settings, {"github": adapter, "gmail": adapter})
    return app, TestClient(app), VerificationWorker(app.state.store, app.state.engine, recovery=app.state.recovery), adapter


def original(client, body=None):
    response = client.post("/v1/verify", headers=A, json=body or payload())
    assert response.status_code == 200, response.text
    return response.json()


def again(client, receipt_id, key="retry", body=None, headers=None):
    return client.post(f"/v1/receipts/{receipt_id}/reverify", json=body or {},
                       headers=headers or {**A, "Idempotency-Key": key})


def completed(app, client, worker, receipt, key="retry"):
    response = again(client, receipt["receipt_id"], key)
    assert response.status_code == 202, response.text
    row = run(worker, response.json()["id"])
    assert row["state"] == "COMPLETE", row["terminal_reason"]
    return app.state.store.get_receipt("tenant-a", row["receipt_id"]).model_dump(mode="json")


def test_fresh_observations_link_signed_receipts_and_preserve_original(recovery_app):
    app, client, worker, adapter = recovery_app
    receipt = original(client, payload(2))
    exact = app.state.store.get_receipt("tenant-a", receipt["receipt_id"]).model_dump_json()
    assert receipt["schema_version"] == "1.1" and receipt["verdict"] == "FAILED"
    assert receipt["remediation"][0]["expected"] is True
    assert receipt["remediation"][0]["observed"] is False
    assert receipt["remediation"][0]["retryable"] is True
    assert receipt["remediation"][0]["reverify_after"] == "external_action"
    adapter.state = {"ok": True}
    result = completed(app, client, worker, receipt)
    assert len(adapter.calls) == 4  # every condition is independently read again
    assert result["verdict"] == "VERIFIED" and result["remediation"] == []
    assert result["previous_receipt_id"] == receipt["receipt_id"]
    assert result["previous_receipt_hash"] == receipt["receipt_hash"]
    assert result["contract_hash"] == receipt["contract_hash"]
    assert result["recovery"] == {"chain_id": receipt["receipt_id"], "attempt": 1,
                                  "oscillating_conditions": [], "repeated_failures": []}
    assert app.state.store.get_receipt("tenant-a", receipt["receipt_id"]).model_dump_json() == exact
    assert ReceiptSigner.verify(VerificationReceipt.model_validate(result))
    tampered = {**result, "previous_receipt_hash": "0" * 64}
    assert not ReceiptSigner.verify(VerificationReceipt.model_validate(tampered))
    history = client.get(f"/v1/receipts/{receipt['receipt_id']}/history", headers=A).json()
    assert history["chain_integrity"] and not history["can_reverify"]
    assert history["head_id"] == result["receipt_id"] and len(history["receipts"]) == 2
    assert [r["conditions"][0]["status"] for r in history["receipts"]] == ["FAIL", "PASS"]


@pytest.mark.parametrize("claim", ["remediation", "observations", "contract", "executor_claim", "repair", "previous_receipt_id"])
def test_executor_cannot_supply_recovery_evidence(recovery_app, claim):
    _, client, _, adapter = recovery_app
    receipt = original(client)
    response = again(client, receipt["receipt_id"], body={claim: {"ok": True, "access_token": "secret-sentinel"}})
    assert response.status_code == 422 and "secret-sentinel" not in response.text
    assert len(adapter.calls) == 1


def test_guidance_is_not_observed_state_even_when_provider_echoes_it(recovery_app):
    app, client, worker, adapter = recovery_app
    receipt = original(client)
    adapter.state = {"ok": True, "copied": receipt["remediation"]}
    result = completed(app, client, worker, receipt)
    assert result["verdict"] == "UNKNOWN"
    assert result["results"][0]["evidence"]["observed"] is None
    adapter.state = receipt
    result = completed(app, client, worker, result, "second")
    assert result["verdict"] == "UNKNOWN"


@pytest.mark.parametrize("path", ["remediation.expected", "payload.action_hint", "recovery.attempt", "previous_receipt_id"])
def test_predicates_cannot_certify_reserved_guidance_fields(recovery_app, path):
    _, client, _, adapter = recovery_app
    adapter.state = {"remediation": {"expected": True}, "payload": {"action_hint": True},
                     "recovery": {"attempt": True}, "previous_receipt_id": True}
    body = payload()
    body["contract"]["postconditions"][0]["predicate"]["path"] = path
    assert original(client, body)["verdict"] == "UNKNOWN"


def test_remediation_is_deterministic_redacted_and_draft_specific(recovery_app):
    app, client, _, adapter = recovery_app
    adapter.state = {"location": "DRAFT"}
    body = payload(provider="gmail")
    body["contract"]["postconditions"][0]["predicate"] = {"op": "eq", "path": "location", "expected": "SENT"}
    root = original(client, body)
    receipt = app.state.store.get_receipt("tenant-a", root["receipt_id"])
    entry = remediation_for(receipt.results)[0]
    assert entry.action_hint == "The message exists but is not in SENT state."
    assert entry == remediation_for(receipt.results)[0]
    receipt.results[0].predicate.path = "access_token"
    receipt.results[0].predicate.expected = "expected-secret"
    receipt.results[0].evidence.observed = "observed-secret"
    output = remediation_for(receipt.results)[0].model_dump_json()
    assert "expected-secret" not in output and "observed-secret" not in output
    assert not remediation_for(receipt.results)[0].retryable


def test_tenant_isolation_and_no_unauthenticated_recovery(recovery_app):
    _, client, _, _ = recovery_app
    root = original(client)["receipt_id"]
    for suffix in ("history", "remediation"):
        assert client.get(f"/v1/receipts/{root}/{suffix}", headers=B).status_code == 404
        assert client.get(f"/v1/receipts/{root}/{suffix}").status_code == 401
    assert again(client, root, headers=B).status_code == 404
    assert again(client, root, headers={"Idempotency-Key": "retry"}).status_code == 401
    assert client.post(f"/v1/receipts/{root}/recovery-policy", headers=B, json={"automatic": True}).status_code == 404
    assert again(client, root, headers={"X-DoneProof-Key": "admin-a", "Idempotency-Key": "retry"}).status_code == 401


def test_concurrent_retries_idempotency_stale_heads_and_duplicate_signing(recovery_app):
    app, client, worker, _ = recovery_app
    root = original(client)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: again(client, root["receipt_id"]), range(6)))
    assert {r.status_code for r in responses} <= {200, 202}
    ids = {r.json()["id"] for r in responses}
    assert len(ids) == 1
    assert again(client, root["receipt_id"], "different").json()["detail"] == "reverification_in_progress"
    assert again(client, root["receipt_id"], body={"deadline_seconds": 30}).status_code == 409
    row = run(worker, ids.pop())
    before = len(app.state.store.list_receipts("tenant-a"))
    assert again(client, root["receipt_id"]).json()["id"] == row["id"]
    assert again(client, root["receipt_id"], "new").json()["detail"] == "receipt_is_not_chain_head"
    # A stale signing owner cannot publish another receipt after completion.
    worker.db.publish(row, app.state.engine)
    assert len(app.state.store.list_receipts("tenant-a")) == before == 2


def test_original_registered_baseline_is_frozen(recovery_app):
    app, client, worker, adapter = recovery_app
    registered = client.post("/v1/runs", headers=A, json=payload(change=True)).json()
    receipt = client.post(f"/v1/runs/{registered['id']}/verify", headers=A).json()
    assert receipt["results"][0]["baseline_status"] == "FAIL"
    # Simulate later baseline maintenance. Recovery uses the original signed baseline.
    baseline = app.state.store.get_baselines("tenant-a", registered["id"])["p0"]
    baseline.status = ConditionStatus.PASS
    baseline.evidence.observed = True
    app.state.store.save_baseline("tenant-a", registered["id"], baseline)
    adapter.state = {"ok": True}
    result = completed(app, client, worker, receipt)
    assert result["verdict"] == "VERIFIED" and result["assurance_level"] == "registered"
    assert result["results"][0]["baseline_status"] == "FAIL"
    assert all(context.task_started_at == registered["task_started_at"].replace("Z", "+00:00") for _, context in adapter.calls)


@pytest.mark.parametrize("baseline", ["already_satisfied", "unknown", "unregistered"])
def test_reverification_cannot_repair_missing_transition_proof(recovery_app, baseline):
    app, client, worker, adapter = recovery_app
    adapter.state = {"ok": True}
    adapter.unknown = baseline == "unknown"
    if baseline == "unregistered":
        receipt = original(client, payload(change=True))
    else:
        registered = client.post("/v1/runs", headers=A, json=payload(change=True)).json()
        adapter.unknown = False
        receipt = client.post(f"/v1/runs/{registered['id']}/verify", headers=A).json()
    assert receipt["remediation"][0]["retryable"] is False
    assert receipt["remediation"][0]["reverify_after"] == "new_registered_run"
    result = completed(app, client, worker, receipt)
    assert result["verdict"] == ("FAILED" if baseline == "already_satisfied" else "UNKNOWN")


def test_cancellation_and_expiry_consume_budget_without_forking_chain(recovery_app):
    app, client, worker, _ = recovery_app
    app.state.recovery.max_attempts = 2
    root = original(client)
    first = again(client, root["receipt_id"]).json()["id"]
    assert client.post(f"/v1/jobs/{first}/cancel", headers=A).json()["state"] == "EXPIRED"
    second = again(client, root["receipt_id"], "second").json()["id"]
    update(worker.db, "UPDATE verification_jobs SET deadline_at=0 WHERE tenant_id=? AND id=?", ("tenant-a", second))
    assert client.get(f"/v1/jobs/{second}", headers=A).json()["state"] == "EXPIRED"
    response = again(client, root["receipt_id"], "third")
    assert response.status_code == 409 and response.json()["detail"] == "reverification_limit_reached"
    history = app.state.recovery.history("tenant-a", root["receipt_id"])
    assert history["attempts_used"] == 2 and len(history["receipts"]) == 1
    assert history["active_job_id"] is None


def test_job_admission_rolls_back_attempt_reservation(recovery_app, monkeypatch):
    app, client, worker, _ = recovery_app
    root = original(client)
    create = app.state.recovery.create_in_transaction
    def fail(*args, **kwargs):
        create(*args, **kwargs)
        raise RuntimeError("simulated interrupted admission")
    with monkeypatch.context() as patch:
        patch.setattr(app.state.recovery, "create_in_transaction", fail)
        with pytest.raises(RuntimeError):
            again(client, root["receipt_id"])
    history = app.state.recovery.history("tenant-a", root["receipt_id"])
    assert history["attempts_used"] == 0 and history["active_job_id"] is None
    assert completed(app, client, worker, root)["recovery"]["attempt"] == 1


def test_crash_during_publication_is_recoverable_and_atomic(recovery_app, monkeypatch):
    app, client, worker, _ = recovery_app
    root = original(client)
    identifier = again(client, root["receipt_id"]).json()["id"]
    while worker.db.get_job("tenant-a", identifier)["state"] != "SIGNING":
        asyncio.run(worker.tick())
    owner = worker.db.claim(90)
    finish = RecoveryStore.finish_publication
    def crash(*args):
        finish(*args)
        raise RuntimeError("simulated process interruption before commit")
    with monkeypatch.context() as patch:
        patch.setattr(RecoveryStore, "finish_publication", crash)
        with pytest.raises(RuntimeError):
            worker.db.publish(owner, app.state.engine)
    assert len(app.state.store.list_receipts("tenant-a")) == 1
    update(worker.db, "UPDATE verification_jobs SET lease_until=0 WHERE tenant_id=? AND id=?", ("tenant-a", identifier))
    result = run(VerificationWorker(app.state.store, app.state.engine), identifier)
    assert result["state"] == "COMPLETE"
    assert len(app.state.recovery.history("tenant-a", root["receipt_id"])["receipts"]) == 2


def test_older_workers_cannot_publish_unlinked_receipts_or_strand_terminal_jobs(recovery_app):
    app, client, worker, _ = recovery_app
    root = original(client)
    identifier = again(client, root["receipt_id"]).json()["id"]
    job = worker.db.get_job("tenant-a", identifier)
    # Model the old worker's standalone publication, which ignores recovery metadata.
    unlinked = VerificationReceipt.model_validate(root)
    unlinked.receipt_id = job["receipt_id"]
    unlinked.recovery.chain_id = job["receipt_id"]
    app.state.signer.sign(unlinked)
    with pytest.raises(Exception, match="Recovery publication requires linked receipt"):
        app.state.store.save_receipt("tenant-a", unlinked)
    update(worker.db, """UPDATE verification_jobs SET state='INTERNAL_ERROR',finished_at=1,
        terminal_reason='internal_error' WHERE tenant_id=? AND id=?""", ("tenant-a", identifier))
    history = app.state.recovery.history("tenant-a", root["receipt_id"])
    assert history["active_job_id"] is None and history["attempts_used"] == 1
    assert len(history["receipts"]) == 1


def test_oscillating_and_repeated_failures_are_signed(recovery_app):
    app, client, worker, adapter = recovery_app
    body = payload()
    body["contract"]["postconditions"][0]["predicate"] = {"op": "eq", "path": "value", "expected": 3}
    adapter.state = {"value": 1}
    receipt = original(client, body)
    for index, value in enumerate([2, 1, 2]):
        adapter.state = {"value": value}
        receipt = completed(app, client, worker, receipt, f"retry-{index}")
    assert receipt["recovery"]["oscillating_conditions"] == ["p0"]
    for index in range(2):
        receipt = completed(app, client, worker, receipt, f"stable-{index}")
    assert receipt["recovery"]["repeated_failures"] == ["p0"]
    assert receipt["recovery"]["oscillating_conditions"] == ["p0"]
    assert ReceiptSigner.verify(VerificationReceipt.model_validate(receipt))


def test_large_durable_contract_can_be_reverified(recovery_app):
    app, client, worker, adapter = recovery_app
    identifier = submit(client, payload(100))
    first = run(worker, identifier)
    root = app.state.store.get_receipt("tenant-a", first["receipt_id"]).model_dump(mode="json")
    adapter.state = {"ok": True}
    result = completed(app, client, worker, root)
    assert result["summary"]["passed"] == 100 and len(adapter.calls) == 200


def test_unknown_retry_preserves_unknown_semantics(recovery_app):
    app, client, worker, adapter = recovery_app
    adapter.unknown = True
    root = original(client)
    result = completed(app, client, worker, root)
    assert result["verdict"] == "UNKNOWN" and result["summary"]["passed"] == 0
    assert result["remediation"][0]["reverify_after"] == "authoritative_evidence"


@pytest.mark.parametrize("limit", [-1, 21, True, 1.5])
def test_invalid_limits_fail_startup(auth_settings, limit):
    with pytest.raises(ValueError, match="Re-verification limit"):
        create_app(replace(auth_settings, max_reverification_attempts=limit))


def test_console_contains_recovery_controls_and_renders_values_as_text(recovery_app):
    _, client, _, _ = recovery_app
    assert 'src="/console/recovery.js"' in client.get("/console").text
    assert "Verification history" in client.get("/console").text
    script = client.get("/console/recovery.js").text
    assert "node.textContent = text" in script and "innerHTML" not in script
    assert "crypto.randomUUID()" in script and "after_revision" in script
    assert "new_registered_run" not in script  # data is presented, never interpreted as execution


def test_patterns_do_not_treat_distinct_unknowns_as_confirmed_progress(recovery_app):
    app, client, _, _ = recovery_app
    receipt = app.state.store.get_receipt("tenant-a", original(client)["receipt_id"])
    receipts = [receipt.model_copy(deep=True) for _ in range(4)]
    for index, item in enumerate(receipts):
        item.results[0].status = ConditionStatus.UNKNOWN if index % 2 else ConditionStatus.FAIL
    assert failure_patterns(receipts)[0] == ["p0"]
    assert json.loads(receipt.model_dump_json())["summary"]["passed"] == 0
