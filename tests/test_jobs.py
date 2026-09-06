from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from doneproof.adapters.base import ProviderAdapter, ProviderObservation
from doneproof.app import create_app
from doneproof.domain import CompletionContract
from doneproof.job_models import JobContract
from doneproof.job_store import JobStore
from doneproof.retries import TransientObservationError
from doneproof.signing import ReceiptSigner
from doneproof.store import Store
from doneproof.worker import VerificationWorker

A = {"X-DoneProof-Key": "key-a", "Idempotency-Key": "job-test"}
B = {"X-DoneProof-Key": "key-b", "Idempotency-Key": "job-test"}


class Provider(ProviderAdapter):
    def __init__(self):
        self.calls = []
        self.mode = "pass"
        self.delay = 0
        self.active = self.peak = 0

    async def observe(self, selector, context):
        self.calls.append((context.tenant_id, context.condition_id))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.mode == "transient":
                raise TransientObservationError(retry_after=30)
            return ProviderObservation({"ok": self.mode == "pass", "access_token": "provider-secret-sentinel"},
                                       indeterminate=self.mode == "unknown")
        finally:
            self.active -= 1


def payload(size=1, *, provider="github", change=False):
    return {"contract": {"task": "Verify a durable outcome", "postconditions": [
        {"id": f"p{i}", "description": "Expected authoritative state", "provider": provider,
         "selector": {"number": i + 1}, "predicate": {"op": "eq", "path": "ok", "expected": True},
         "require_change": change} for i in range(size)]}}


@pytest.fixture
def jobs(connection_settings):
    adapter = Provider()
    app = create_app(connection_settings, {"github": adapter, "gmail": adapter})
    client = TestClient(app)
    worker = VerificationWorker(app.state.store, app.state.engine)
    return app, client, worker, adapter


def submit(client, body=None, headers=None):
    response = client.post("/v1/jobs", json=body or payload(), headers=headers or A)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def update(db, sql, args=()):
    with db.transaction() as con:
        db.execute(con, sql, args)


def wake(db, job_id):
    update(db, "UPDATE verification_jobs SET next_run_at=0 WHERE id=?", (job_id,))
    update(db, "UPDATE verification_conditions SET next_attempt_at=0 WHERE job_id=?", (job_id,))


def run(worker, identifier):
    return asyncio.run(worker.run_until_terminal("tenant-a", identifier))


def test_state_machine_checkpoints_and_receipt_are_compatible(jobs):
    app, client, worker, adapter = jobs
    identifier = submit(client, payload(10))
    states = [worker.db.get_job("tenant-a", identifier)["state"]]
    for _ in range(5):
        asyncio.run(worker.tick())
        state = worker.db.get_job("tenant-a", identifier)["state"]
        if state != states[-1]:
            states.append(state)
    assert states == ["QUEUED", "OBSERVING", "EVALUATING", "SIGNING", "COMPLETE"]
    public = client.get(f"/v1/jobs/{identifier}", headers=A).json()
    receipt = app.state.store.get_receipt("tenant-a", public["receipt_id"])
    assert receipt.verdict.value == "VERIFIED" and ReceiptSigner.verify(receipt)
    assert len(adapter.calls) == 10 and adapter.peak <= 8
    rows = client.get(f"/v1/jobs/{identifier}/conditions?offset=2&limit=3", headers=A).json()["conditions"]
    assert [r["id"] for r in rows] == ["p2", "p3", "p4"]
    assert all(r["attempts"] == 1 and r["executions"][0]["outcome"] == "observed" for r in rows)
    public_text = json.dumps(rows) + json.dumps(public) + receipt.model_dump_json()
    assert "provider-secret-sentinel" not in public_text
    assert "observation_json" not in public_text and "lease_token" not in public_text
    assert all("provider-secret-sentinel" not in r["observation_json"] for r in worker.db.conditions("tenant-a", identifier))
    assert client.get(f"/v1/receipts/{receipt.receipt_id}", headers=A).status_code == 200


def test_idempotency_uses_raw_request_before_generated_contract_defaults(jobs):
    _, client, worker, _ = jobs
    identifier = submit(client)
    assert client.post("/v1/jobs", json=payload(), headers=A).json()["id"] == identifier
    assert client.post("/v1/jobs", json=payload(2), headers=A).status_code == 409
    other = submit(client, headers=B)
    assert other != identifier
    run(worker, identifier)
    repeated = client.post("/v1/jobs", json=payload(), headers=A)
    assert repeated.status_code == 200 and repeated.json()["state"] == "COMPLETE"


def test_concurrent_creation_is_atomic(jobs):
    app, _, worker, _ = jobs
    contract = JobContract.model_validate(payload()["contract"])
    def create(_):
        return JobStore(app.state.store).create("tenant-a", "concurrent", "request-hash", contract, {}, "submitted", 300)
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(create, range(12)))
    assert len({row[0]["id"] for row in rows}) == 1
    assert sum(created for _, created in rows) == 1
    assert len(worker.db.conditions("tenant-a", rows[0][0]["id"])) == 1


def test_auth_isolation_input_limits_and_unknown_claims(jobs):
    app, client, worker, adapter = jobs
    identifier = submit(client)
    for suffix in ("", "/conditions", "/wait"):
        assert client.get(f"/v1/jobs/{identifier}{suffix}", headers=B).status_code == 404
        assert client.get(f"/v1/jobs/{identifier}{suffix}").status_code == 401
    assert client.post(f"/v1/jobs/{identifier}/cancel", headers=B).status_code == 404
    assert client.post("/v1/jobs", json=payload(), headers={"X-DoneProof-Key": "admin-a"}).status_code == 401
    assert client.post("/v1/jobs", json=payload(), headers={"X-DoneProof-Key": "key-a"}).status_code == 400
    assert client.post("/v1/jobs", json=payload(1001), headers=A).status_code == 422
    body = payload()
    body["observations"] = {"ok": True, "secret": "request-secret-sentinel"}
    response = client.post("/v1/jobs", json=body, headers={**A, "Idempotency-Key": "claims"})
    assert response.status_code == 422 and "request-secret-sentinel" not in response.text
    body = payload(provider="unresolved")
    body["contract"]["postconditions"][0]["selector"] = {"url": "https://example.org/arbitrary", "ok": True}
    unknown = submit(client, body, {**A, "Idempotency-Key": "unknown"})
    row = run(worker, unknown)
    assert app.state.store.get_receipt("tenant-a", row["receipt_id"]).verdict.value == "UNKNOWN"
    assert all(condition == "p0" for _, condition in adapter.calls)


@pytest.mark.parametrize("mode,verdict", [("fail", "FAILED"), ("unknown", "UNKNOWN")])
def test_semantic_failures_complete_without_retries(jobs, mode, verdict):
    app, client, worker, adapter = jobs
    adapter.mode = mode
    identifier = submit(client)
    row = run(worker, identifier)
    assert row["state"] == "COMPLETE"
    assert app.state.store.get_receipt("tenant-a", row["receipt_id"]).verdict.value == verdict
    assert len(adapter.calls) == 1


def test_transient_retry_is_durable_delayed_and_exhaustion_stays_unknown(jobs):
    app, client, worker, adapter = jobs
    adapter.mode = "transient"
    identifier = submit(client)
    before = time.time()
    asyncio.run(worker.tick())
    condition = worker.db.conditions("tenant-a", identifier)[0]
    assert condition["state"] == "PENDING" and condition["next_attempt_at"] >= before + 30
    asyncio.run(worker.tick())
    assert len(adapter.calls) == 1
    # A new process resumes persisted policy attempts; it never resets the retry budget.
    for _ in range(3):
        wake(worker.db, identifier)
        restarted = VerificationWorker(app.state.store, app.state.engine)
        asyncio.run(restarted.tick())
    row = run(worker, identifier)
    assert row["state"] == "PARTIAL_FAILURE" and len(adapter.calls) == 4
    receipt = app.state.store.get_receipt("tenant-a", row["receipt_id"])
    assert receipt.verdict.value == "UNKNOWN" and receipt.summary.unknown == 1
    assert worker.db.conditions("tenant-a", identifier)[0]["infrastructure_failure"] == 1


def test_crash_recovery_fences_old_worker_and_reuses_observations(jobs):
    app, client, worker, adapter = jobs
    identifier = submit(client, payload(2))
    old_job = worker.db.claim(90)
    old_claim = worker.db.claim_conditions(old_job, 1, 90)[0]
    update(worker.db, "UPDATE verification_jobs SET lease_until=0 WHERE id=?", (identifier,))
    new_job = worker.db.claim(90)
    assert new_job["lease_token"] != old_job["lease_token"]
    new_claim = worker.db.claim_conditions(new_job, 1, 90)[0]
    observation = asyncio.run(app.state.engine.observe(JobContract.model_validate(payload()["contract"]).postconditions[0],
                               JobContract.model_validate(payload()["contract"]), "tenant-a", durable=True))
    worker.db.finish_observations(old_job, [(old_claim, observation, None, 0)])
    assert worker.db.conditions("tenant-a", identifier)[0]["state"] == "RUNNING"
    worker.db.finish_observations(new_job, [(new_claim, observation, None, 0)])
    worker.db.release_slots([old_claim, new_claim])
    assert worker.db.conditions("tenant-a", identifier)[0]["attempts"] == 2
    result = run(VerificationWorker(app.state.store, app.state.engine), identifier)
    assert result["state"] == "COMPLETE" and len(adapter.calls) == 2


def test_global_provider_limits_across_jobs_and_worker_instances(jobs):
    app, client, worker, adapter = jobs
    adapter.delay = 0.03
    first = submit(client, payload(20))
    second = submit(client, payload(20), {**A, "Idempotency-Key": "second"})
    async def concurrent():
        await asyncio.gather(worker.run_until_terminal("tenant-a", first),
                            VerificationWorker(app.state.store, app.state.engine).run_until_terminal("tenant-a", second))
    asyncio.run(concurrent())
    assert adapter.peak <= 8 and len(adapter.calls) == 40


def test_cancel_stops_inflight_reads_and_blocks_publication(jobs):
    app, client, worker, adapter = jobs
    adapter.delay = 5
    identifier = submit(client, payload(10))
    async def cancel():
        task = asyncio.create_task(worker.tick())
        while adapter.active == 0:
            await asyncio.sleep(0.005)
        assert worker.db.cancel("tenant-a", identifier)["terminal_reason"] == "cancelled"
        await asyncio.wait_for(task, 2)
    asyncio.run(cancel())
    assert adapter.active == 0
    assert app.state.store.list_receipts("tenant-a") == []
    assert client.post(f"/v1/jobs/{identifier}/cancel", headers=A).json()["state"] == "EXPIRED"
    assert all(r["state"] == "ABORTED" for r in worker.db.conditions("tenant-a", identifier))
    with worker.db.transaction() as con:
        assert not worker.db.execute(con, "SELECT 1 FROM verification_provider_slots WHERE lease_token IS NOT NULL").fetchone()


@pytest.mark.parametrize("stage", ["QUEUED", "OBSERVING", "EVALUATING", "SIGNING"])
def test_deadline_and_cancellation_at_each_stage_never_sign(jobs, stage):
    app, client, worker, _ = jobs
    identifier = submit(client, payload(10))
    while worker.db.get_job("tenant-a", identifier)["state"] != stage:
        asyncio.run(worker.tick())
    update(worker.db, "UPDATE verification_jobs SET deadline_at=0 WHERE id=?", (identifier,))
    assert client.get(f"/v1/jobs/{identifier}", headers=A).json()["state"] == "EXPIRED"
    asyncio.run(worker.tick())
    assert not app.state.store.list_receipts("tenant-a")


def test_signing_retry_after_crash_publishes_only_one_receipt(jobs):
    app, client, worker, adapter = jobs
    identifier = submit(client)
    asyncio.run(worker.tick())
    asyncio.run(worker.tick())
    old = worker.db.claim(90)
    assert old["state"] == "SIGNING"
    update(worker.db, "UPDATE verification_jobs SET lease_until=0 WHERE id=?", (identifier,))
    new = worker.db.claim(90)
    worker.db.publish(old, app.state.engine)
    assert not app.state.store.list_receipts("tenant-a")
    worker.db.publish(new, app.state.engine)
    worker.db.publish(new, app.state.engine)
    assert len(app.state.store.list_receipts("tenant-a")) == 1 and len(adapter.calls) == 1
    assert worker.db.cancel("tenant-a", identifier)["state"] == "COMPLETE"


def test_signing_transaction_rollback_and_key_rotation_fail_closed(jobs, monkeypatch):
    app, client, worker, _ = jobs
    identifier = submit(client)
    asyncio.run(worker.tick())
    asyncio.run(worker.tick())
    job = worker.db.claim(90)
    original = worker.db._terminal
    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash after receipt insert")
    monkeypatch.setattr(worker.db, "_terminal", crash)
    with pytest.raises(RuntimeError):
        worker.db.publish(job, app.state.engine)
    assert not app.state.store.list_receipts("tenant-a")
    monkeypatch.setattr(worker.db, "_terminal", original)
    app.state.engine.signer = ReceiptSigner(replace(app.state.settings, signing_seed_b64=None, legacy_receipt_key="rotated"))
    worker.db.publish(job, app.state.engine)
    assert worker.db.get_job("tenant-a", identifier)["terminal_reason"] == "signing_key_changed"
    assert not app.state.store.list_receipts("tenant-a")


def test_registered_transition_boundary_and_submitted_contracts(jobs):
    app, client, worker, adapter = jobs
    adapter.mode = "fail"
    body = payload(change=True)
    registered = client.post("/v1/runs", headers=A, json=body).json()
    adapter.mode = "pass"
    identifier = submit(client, {"registered_contract_id": registered["id"]})
    row = run(worker, identifier)
    receipt = app.state.store.get_receipt("tenant-a", row["receipt_id"])
    assert receipt.verdict.value == "VERIFIED" and receipt.assurance_level == "registered"
    assert receipt.results[0].baseline_status.value == "FAIL"
    submitted = submit(client, body, {**A, "Idempotency-Key": "submitted"})
    row = run(worker, submitted)
    assert app.state.store.get_receipt("tenant-a", row["receipt_id"]).verdict.value == "UNKNOWN"
    unregistered = CompletionContract.model_validate(payload()["contract"])
    app.state.store.save_contract("tenant-a", unregistered)
    assert client.post("/v1/jobs", json={"registered_contract_id": unregistered.id},
                       headers={**A, "Idempotency-Key": "unregistered"}).status_code == 404
    assert client.post("/v1/jobs", json={"registered_contract_id": registered["id"]}, headers=B).status_code == 404


def test_long_poll_observes_revision_and_has_bounded_timeout(jobs):
    _, client, worker, _ = jobs
    identifier = submit(client)
    response = client.get(f"/v1/jobs/{identifier}/wait?after_revision=0&timeout=0.02", headers=A)
    assert response.json()["state"] == "QUEUED"
    asyncio.run(worker.tick())
    assert client.get(f"/v1/jobs/{identifier}/wait?after_revision=0", headers=A).json()["revision"] > 0
    assert client.get(f"/v1/jobs/{identifier}/wait?timeout=26", headers=A).status_code == 422


def test_thousand_conditions_async_does_not_change_sync_limit(jobs):
    app, client, worker, adapter = jobs
    body = payload(1000)
    assert client.post("/v1/verify", json=body, headers=A).status_code == 422
    identifier = submit(client, body)
    row = run(worker, identifier)
    receipt = app.state.store.get_receipt("tenant-a", row["receipt_id"])
    assert row["state"] == "COMPLETE" and receipt.summary.total == 1000 and len(adapter.calls) == 1000
    assert ReceiptSigner.verify(receipt)


def test_migration_preserves_receipt_bytes_and_is_concurrent(jobs):
    app, client, worker, _ = jobs
    # Simulate an existing production receipt, then start multiple processes against the same database.
    receipt = client.post("/v1/verify", json=payload(), headers=A).json()
    with worker.db.transaction() as con:
        before = worker.db.execute(con, "SELECT body_json FROM receipts WHERE receipt_id=?", (receipt["receipt_id"],)).fetchone()["body_json"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: Store(app.state.settings.storage_dsn), range(8)))
    with worker.db.transaction() as con:
        after = worker.db.execute(con, "SELECT body_json FROM receipts WHERE receipt_id=?", (receipt["receipt_id"],)).fetchone()["body_json"]
        if worker.db.pg:
            assert worker.db.execute(con, "SELECT version FROM schema_migrations WHERE version=3").fetchone()
    assert before == after
    assert run(worker, submit(client))["state"] == "COMPLETE"


def test_graceful_stop_releases_reads_and_recovery_completes(jobs):
    app, client, worker, adapter = jobs
    identifier = submit(client)
    adapter.delay = 5
    async def stop():
        task = asyncio.create_task(worker.tick())
        while not adapter.active:
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(stop())
    assert adapter.active == 0
    adapter.delay = 0
    assert run(VerificationWorker(app.state.store, app.state.engine), identifier)["state"] == "COMPLETE"
    assert worker.db.conditions("tenant-a", identifier)[0]["attempts"] == 2


def test_sensitive_predicates_stay_unknown_after_checkpoint_redaction(jobs):
    app, client, worker, _ = jobs
    body = payload()
    body["contract"]["postconditions"][0]["predicate"] = {"op": "exists", "path": "access_token"}
    row = run(worker, submit(client, body))
    receipt = app.state.store.get_receipt("tenant-a", row["receipt_id"])
    assert receipt.verdict.value == "UNKNOWN"
    assert "provider-secret-sentinel" not in receipt.model_dump_json()


def test_internal_error_is_terminal_and_error_details_are_private(jobs, monkeypatch, caplog):
    app, client, worker, _ = jobs
    identifier = submit(client)
    asyncio.run(worker.tick())
    def fail(*_):
        raise RuntimeError("internal-secret-sentinel")
    monkeypatch.setattr(worker, "_evaluate", fail)
    asyncio.run(worker.tick())
    public = client.get(f"/v1/jobs/{identifier}", headers=A)
    assert public.json()["state"] == "INTERNAL_ERROR"
    assert "internal-secret-sentinel" not in public.text + caplog.text
    assert not app.state.store.list_receipts("tenant-a")


def test_independent_worker_threads_share_provider_slots(jobs):
    app, client, _, adapter = jobs
    adapter.delay = 0.02
    identifiers = [submit(client, payload(12), {**A, "Idempotency-Key": f"thread-{i}"}) for i in range(2)]
    def work(identifier):
        worker = VerificationWorker(app.state.store, app.state.engine)
        return run(worker, identifier)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(work, identifiers))
    assert all(row["state"] == "COMPLETE" for row in results)
    assert len(adapter.calls) == 24 and adapter.peak <= 8


def test_job_openapi_documents_the_async_limit_and_rejects_credentials(jobs):
    app, client, _, _ = jobs
    schema = app.openapi()["paths"]["/v1/jobs"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    contract = schema["properties"]["contract"]["anyOf"][0]
    assert contract["properties"]["postconditions"]["maxItems"] == 1000
    body = payload()
    body["contract"]["postconditions"][0]["selector"]["access_token"] = "caller-secret-sentinel"
    response = client.post("/v1/jobs", json=body, headers=A)
    assert response.status_code == 422 and "caller-secret-sentinel" not in response.text
