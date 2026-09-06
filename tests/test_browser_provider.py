import asyncio
import json
from dataclasses import replace

import pytest

from doneproof.browser_models import BrowserProvenance
from doneproof.browser_network import BrowserUnavailable
from doneproof.browser_policy import BrowserChecks
from doneproof.domain import VerificationReceipt
from doneproof.signing import ReceiptSigner
from tests.browser_helpers import CHECK, PNG, app_for, payload, selector
from tests.test_jobs import A, B, run, submit


@pytest.fixture
def browser_app(connection_settings):
    return app_for(connection_settings)


def test_tenant_catalog_compilation_and_independent_signed_evidence(browser_app, monkeypatch):
    app, client, _, observer = browser_app
    monkeypatch.setattr("doneproof.adapters.browser.importlib.util.find_spec", lambda _: True)
    listing = client.get("/v1/browser/checks", headers=A).json()
    assert listing["checks"][0]["revision"] == selector()["revision"]
    assert client.get("/v1/browser/checks", headers=B).json()["checks"] == []
    assert client.get("/v1/browser/checks").status_code == 401
    task = f'Verify browser check "release-7" at revision "{selector()["revision"]}" matches'
    compiled = client.post("/v2/contracts/compile", json={"task": task}, headers=A).json()
    assert compiled["status"] == "valid_contract", compiled
    assert compiled["deterministic"] and compiled["selector_checks"][0]["status"] == "resolved"
    assert "browser_lower_assurance" in {w["code"] for w in compiled["contract_quality"]["warnings"]}
    first = client.post("/v1/verify", json={"contract": compiled["contract"]}, headers=A)
    assert first.status_code == 200, first.text
    receipt = VerificationReceipt.model_validate(first.json())
    assert receipt.schema_version == "1.2" and receipt.verdict == "VERIFIED" and ReceiptSigner.verify(receipt)
    evidence = receipt.results[0].evidence
    assert evidence.observed is True and evidence.source_url == CHECK["url"]
    assert evidence.provenance.assurance == "lower_than_authoritative_api"
    assert evidence.provenance.executor_supplied is False and evidence.provenance.samples == 3
    assert len(observer.calls) == 2  # Compilation preflight never replaces independent verification.
    artifacts = app.state.engine.adapters["browser"].artifacts
    ref = evidence.provenance.screenshot
    assert artifacts.read_for_operator("tenant-a", ref.artifact_id) == PNG
    assert artifacts.read_for_operator("tenant-b", ref.artifact_id) is None
    with artifacts.transaction() as con:
        rows = artifacts.execute(con, "SELECT * FROM browser_artifacts").fetchall()
    assert len(rows) == 1
    assert "iVBOR" not in rows[0]["ciphertext"] and "iVBOR" not in first.text
    receipt.results[0].evidence.provenance.assurance = "authoritative_api"
    assert not ReceiptSigner.verify(receipt)


@pytest.mark.parametrize("outcome", ["login_or_challenge", "blocked_request", "ambiguous_ui", "unstable_ui", "screenshot_unavailable"])
def test_inconclusive_ui_never_passes_or_infers_negative(browser_app, outcome):
    _, client, _, observer = browser_app
    observer.error = BrowserUnavailable(outcome)
    response = client.post("/v1/verify", json=payload(), headers=A)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["verdict"] == "UNKNOWN"
    assert receipt["results"][0]["evidence"]["provenance"]["outcome"] == outcome
    assert receipt["results"][0]["evidence"]["provenance"]["screenshot"] is None


def test_recognized_negative_is_fail_and_repair_reobserves_with_immutable_chain(browser_app):
    app, client, worker, observer = browser_app
    observer.state = "pending"
    first = client.post("/v1/verify", json=payload(), headers=A).json()
    assert first["verdict"] == "FAILED"
    original = app.state.store.get_receipt("tenant-a", first["receipt_id"]).model_dump_json()
    observer.state = "ready"
    request = client.post(f'/v1/receipts/{first["receipt_id"]}/reverify', json={}, headers=A)
    assert request.status_code == 202, request.text
    row = run(worker, request.json()["id"])
    receipt = app.state.store.get_receipt("tenant-a", row["receipt_id"])
    assert receipt.verdict == "VERIFIED" and receipt.previous_receipt_id == first["receipt_id"]
    assert receipt.schema_version == "1.2" and ReceiptSigner.verify(receipt)
    assert receipt.results[0].evidence.provenance.session_id != first["results"][0]["evidence"]["provenance"]["session_id"]
    assert app.state.store.get_receipt("tenant-a", first["receipt_id"]).model_dump_json() == original


def test_durable_restart_tenant_isolation_idempotency_and_freshness(browser_app):
    app, client, worker, observer = browser_app
    identifier = submit(client, payload())
    assert client.post("/v1/jobs", json=payload(), headers=A).json()["id"] == identifier
    asyncio.run(worker.tick())
    # Checkpoints contain provenance, never image bytes or external browser state.
    checkpoint = worker.db.conditions("tenant-a", identifier)[0]["observation_json"]
    assert json.loads(checkpoint)["provenance"]["session_id"].startswith("bo_")
    assert "iVBOR" not in checkpoint
    from doneproof.worker import VerificationWorker
    restarted = VerificationWorker(app.state.store, app.state.engine)
    row = run(restarted, identifier)
    assert row["state"] == "COMPLETE" and len(observer.calls) == 1
    assert client.get(f"/v1/jobs/{identifier}", headers=B).status_code == 404
    receipt = app.state.store.get_receipt("tenant-a", row["receipt_id"])
    assert ReceiptSigner.verify(receipt) and receipt.verdict == "VERIFIED"
    assert app.state.store.get_receipt("tenant-b", row["receipt_id"]) is None


def test_changed_policy_invalidates_selectors_and_publication(browser_app):
    app, client, worker, observer = browser_app
    identifier = submit(client, payload())
    asyncio.run(worker.tick())
    asyncio.run(worker.tick())
    adapter = app.state.engine.adapters["browser"]
    adapter.checks = BrowserChecks({"tenant-a": {"release-7": {**CHECK, "page_text": "Release 8"}}})
    receipt = app.state.store.get_receipt("tenant-a", run(worker, identifier)["receipt_id"])
    assert receipt.verdict == "UNKNOWN" and receipt.results[0].evidence.provenance.outcome == "policy_changed"
    # Submitted old revisions cannot observe a newly configured target either.
    assert client.post("/v1/verify", json=payload(), headers=A).json()["verdict"] == "UNKNOWN"
    assert len(observer.calls) == 1


def test_registered_baseline_requires_independent_false_to_true(browser_app):
    _, client, _, observer = browser_app
    observer.state = "pending"
    registration = client.post("/v1/runs", json=payload(change=True), headers=A)
    assert registration.status_code == 200, registration.text
    observer.state = "ready"
    receipt = client.post(f'/v1/runs/{registration.json()["id"]}/verify', headers=A).json()
    assert receipt["verdict"] == "VERIFIED", receipt
    assert receipt["results"][0]["baseline_status"] == "FAIL"
    assert len(observer.calls) == 2


def test_api_coverage_never_falls_back_to_browser(browser_app):
    app, client, _, observer = browser_app
    for i, changes in enumerate(({"no_authoritative_api": False}, {"authoritative_provider": "inventory"},
                    {"url": "https://github.com/org/repo/issues/7"}, {"url": "https://mail.google.com/mail/u/0"})):
        checks = BrowserChecks({"tenant-a": {"release-7": {**CHECK, **changes}}})
        app.state.engine.adapters["browser"].checks = checks
        request = payload()
        request["contract"]["postconditions"][0]["selector"]["revision"] = checks.get("tenant-a", "release-7").revision
        receipt = client.post("/v1/verify", json=request, headers={**A, "Idempotency-Key": f"coverage-{i}"}).json()
        assert receipt["verdict"] == "UNKNOWN"
        assert receipt["results"][0]["evidence"]["provenance"]["outcome"] == "api_required"
    assert observer.calls == []


@pytest.mark.parametrize("extra", [{"url": "https://arbitrary.example/"}, {"screenshot": "executor-image"},
    {"storage_state": "executor-state"}, {"script": "executor-code"}, {"cookies": [{"name": "private"}]}])
def test_executor_inputs_are_rejected_before_storage_or_observation(browser_app, extra):
    app, client, _, observer = browser_app
    request = payload()
    request["contract"]["postconditions"][0]["selector"].update(extra)
    for endpoint in ("/v1/verify", "/v1/runs", "/v1/jobs"):
        response = client.post(endpoint, json=request, headers=A)
        assert response.status_code == 422, response.text
        assert "executor" not in response.text and "private" not in response.text
    assert observer.calls == []


@pytest.mark.parametrize("predicate", [{"op": "eq", "path": "matched", "expected": False},
    {"op": "eq", "path": "revision", "expected": "claimed"}, {"op": "exists", "path": "matched"},
    {"op": "eq", "path": "action_hint", "expected": "success"}])
def test_metadata_and_guidance_cannot_be_certified_as_outcomes(browser_app, predicate):
    _, client, _, observer = browser_app
    request = payload()
    request["contract"]["postconditions"][0]["predicate"] = predicate
    assert client.post("/v1/verify", json=request, headers=A).status_code == 422
    assert observer.calls == []


def test_missing_tenant_and_missing_encryption_never_observe(browser_app):
    app, client, _, observer = browser_app
    assert client.post("/v1/verify", json=payload(), headers=B).json()["verdict"] == "UNKNOWN"
    app.state.engine.adapters["browser"].artifacts.vault.active_key = None
    assert client.post("/v1/verify", json=payload(), headers=A).json()["verdict"] == "UNKNOWN"
    assert observer.calls == []


def test_browser_exception_is_sanitized(browser_app, caplog):
    _, client, _, observer = browser_app
    observer.error = RuntimeError("private-page-text access_token=private-token")
    response = client.post("/v1/verify", json=payload(), headers=A)
    assert response.json()["verdict"] == "UNKNOWN"
    assert "private-token" not in response.text + caplog.text
    assert "private-page-text" not in response.text + caplog.text


def test_receipt_version_compatibility_and_provenance_binding(browser_app):
    _, client, _, _ = browser_app
    receipt = VerificationReceipt.model_validate(client.post("/v1/verify", json=payload(), headers=A).json())
    for old in ("1.0", "1.1"):
        candidate = receipt.model_copy(deep=True)
        candidate.schema_version = old
        assert not ReceiptSigner.verify(candidate)
    with pytest.raises(ValueError):
        BrowserProvenance(executor_supplied=True)


def test_browser_cancel_closes_observation(connection_settings):
    from doneproof.adapters.base import ObservationContext
    app, _, _, _ = app_for(replace(connection_settings, verification_timeout_seconds=30))
    adapter = app.state.engine.adapters["browser"]
    class Pending:
        closed = False
        async def capture(self, check):
            try:
                await asyncio.sleep(30)
            finally:
                self.closed = True
    observer = Pending()
    adapter.observer = observer
    async def cancel():
        task = asyncio.create_task(adapter.observe(selector(), ObservationContext("tenant-a", "c", "now")))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())
    assert observer.closed
