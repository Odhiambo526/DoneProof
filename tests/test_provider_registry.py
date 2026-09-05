import asyncio
import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from doneproof.app import create_app
from doneproof.compilation_models import Candidate, Intent
from doneproof.contract_analysis import analyze
from doneproof.domain import CompletionContract
from doneproof.provider_registry import ProviderRegistry, default_registry
from doneproof.provider_sdk import ProviderDefinition, ProviderManifest
from doneproof.signing import ReceiptSigner
from doneproof.worker import VerificationWorker
from tests.connection_helpers import ADMIN_A, ADMIN_B, begin, finish
from tests.sdk_provider import ACCESS, PRIVATE, REFRESH, definition, payload, registry
from tests.test_jobs import A, B, run, submit, update, wake


@pytest.fixture
def sdk(connection_settings):
    state = {"calls": [], "active": 0, "peak": 0, "tenant-a": "ready"}
    app = create_app(connection_settings, provider_registry=registry(state))
    client = TestClient(app, base_url="https://testserver")
    query, _ = begin(client, "inventory")
    assert finish(client, query, "inventory").headers["location"] == "/connections#connected"
    return app, client, VerificationWorker(app.state.store, app.state.engine), state


def test_registry_metadata_is_valid_and_cannot_be_mutated():
    catalog = default_registry()
    assert [d.manifest.provider_id for d in catalog] == ["github", "gmail", "webhook"]
    for d in catalog:
        spec = d.manifest
        Draft202012Validator.check_schema(spec.selector_schema)
        Draft202012Validator.check_schema(spec.evidence_schema)
        assert spec.resource_types and spec.authentication.requirements
        assert spec.baseline_support and spec.transition_support
        assert spec.rate_limit.concurrency > 0 and spec.evidence_sensitivity
        spec.selector_schema["properties"]["invented"] = {"type": "string"}
        assert "invented" not in d.manifest.selector_schema["properties"]
    with pytest.raises(TypeError):
        catalog._providers["replacement"] = definition()
    with pytest.raises(ValueError, match="Duplicate"):
        ProviderRegistry([definition(), definition()])


@pytest.mark.parametrize("field,value", [
    ("provider_id", "unresolved"), ("provider_id", "../provider"), ("version", "latest"),
    ("supported_predicates", ["execute"]), ("baseline_support", False),
    ("evidence_schema", {"$ref": "https://untrusted.example/schema"}),
    ("selector_schema", {"type": "object", "additionalProperties": True}),
    ("evidence_sensitivity", "ignore_secrets"),
    ("rate_limit", {"concurrency": 0, "preflight_concurrency": 1, "attempts": 100,
                    "base_seconds": 1, "cap_seconds": 10}),
    ("authentication", {"mode": "managed_oauth", "requirements": [], "authorization_origin": "http://localhost"}),
])
def test_invalid_sdk_declarations_fail_startup(field, value):
    data = definition().manifest.model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValueError):
        ProviderManifest.model_validate(data)


def test_missing_implementation_and_auth_backend_fail_closed():
    d = definition()
    with pytest.raises(ValueError):
        ProviderDefinition(d.manifest, d.adapter_factory, d.compiler)
    with pytest.raises(ValueError):
        ProviderDefinition(d.manifest, d.adapter_factory, object(), connection_factory=d.connection_factory)


def test_fourth_provider_drives_every_consumer(sdk):
    app, client, worker, state = sdk
    assert "inventory" in app.state.engine.adapters
    capabilities = client.get("/v1/capabilities", headers=A).json()["providers"]
    assert capabilities[-1] == {"provider": "inventory", "status": "available",
                                "description": app.state.providers.require("inventory").manifest.description}
    assert client.get("/v1/capabilities", headers=B).json()["providers"][-1]["status"] == "configuration_required"
    docs = client.get("/v1/providers", headers=A).json()
    assert docs["sdk_version"] == 1 and docs["providers"][-1]["provider_id"] == "inventory"
    listing = client.get("/v1/connections", headers=ADMIN_A).json()
    assert [p["provider"] for p in listing["providers"]] == ["gmail", "github", "inventory"]
    assert listing["connections"][0]["provider"] == "inventory"
    assert listing["providers"][-1] == {"provider": "inventory", "onboarding_available": True, "installation_url": None}
    assert client.get("/v1/connections", headers=ADMIN_B).json()["connections"] == []
    metadata = client.get("/v1/connections/provider-metadata", headers=ADMIN_A).json()["providers"]
    assert metadata[-1]["authorization_origin"] == "https://inventory.example"
    condition_schema = app.state.compiler.model.schema["properties"]["postconditions"]["items"]["properties"]
    assert "inventory" in condition_schema["provider"]["enum"]
    assert "item_id" in condition_schema["selector"]["properties"]
    compiled = client.post("/v2/contracts/compile", json={"task": "Verify inventory item item-0 is ready"}, headers=A)
    assert compiled.status_code == 200, compiled.text
    result = compiled.json()
    assert result["status"] == "valid_contract" and result["deterministic"]
    assert result["selector_checks"][0]["status"] == "resolved"
    receipt = client.post("/v1/verify", json={"contract": result["contract"]}, headers=A)
    assert receipt.status_code == 200 and receipt.json()["verdict"] == "VERIFIED"
    assert state["calls"][-1][2] != state["calls"][-2][2]  # Preflight is not evidence for verification.
    identifier = submit(client, payload(10))
    row = run(worker, identifier)
    signed = app.state.store.get_receipt("tenant-a", row["receipt_id"])
    assert row["state"] == "COMPLETE" and ReceiptSigner.verify(signed)
    assert state["peak"] <= 2
    assert client.post("/v1/jobs", json=payload(10), headers=A).json()["id"] == identifier
    assert client.get(f"/v1/jobs/{identifier}", headers=B).status_code == 404
    serialized = json.dumps(listing) + json.dumps(docs) + receipt.text + signed.model_dump_json()
    for condition in worker.db.conditions("tenant-a", identifier):
        serialized += condition["observation_json"]
    for secret in (ACCESS, REFRESH, PRIVATE):
        assert secret not in serialized


def test_fourth_provider_refresh_disable_and_publication_fencing(sdk):
    app, client, worker, _ = sdk
    service = app.state.connections
    row = service.db.get("tenant-a", provider="inventory")
    service.db.update(row, expires_at=int(time.time()) - 1)
    usable = asyncio.run(service.usable("tenant-a", "inventory"))
    assert usable[1]["access_token"] == ACCESS + "-rotated"
    identifier = submit(client, payload())
    for _ in range(2):
        asyncio.run(worker.tick())
    assert worker.db.get_job("tenant-a", identifier)["state"] == "SIGNING"
    response = client.post(f"/v1/connections/{row['id']}/disconnect", headers=ADMIN_A)
    assert response.status_code == 200 and response.json()["state"] == "disabled"
    receipt = app.state.store.get_receipt("tenant-a", run(worker, identifier)["receipt_id"])
    assert receipt.verdict.value == "UNKNOWN"
    assert service.db.get("tenant-a", provider="inventory")["credential_ciphertext"] is None


def test_fourth_provider_declared_retries_and_semantic_failures(sdk):
    app, client, worker, state = sdk
    state["transient"] = True
    identifier = submit(client, payload())
    asyncio.run(worker.tick())
    first = worker.db.conditions("tenant-a", identifier)[0]
    assert first["attempts"] == 1 and first["next_attempt_at"] >= time.time() + 43
    wake(worker.db, identifier)
    result = run(worker, identifier)
    assert result["state"] == "PARTIAL_FAILURE"
    assert worker.db.conditions("tenant-a", identifier)[0]["attempts"] == 2
    state["transient"] = False
    state["tenant-a"] = "pending"
    identifier = submit(client, payload(), {**A, "Idempotency-Key": "semantic"})
    result = run(worker, identifier)
    assert result["state"] == "COMPLETE"
    assert app.state.store.get_receipt("tenant-a", result["receipt_id"]).verdict.value == "FAILED"
    assert worker.db.conditions("tenant-a", identifier)[0]["attempts"] == 1


@pytest.mark.parametrize("change", ["version", "missing", "policy"])
def test_worker_rejects_changed_or_removed_provider(sdk, change):
    app, client, _, state = sdk
    identifier = submit(client, payload())
    if change == "missing":
        replacement = default_registry()
    elif change == "version":
        replacement = registry(state, version="2.0.0")
    else:
        replacement = registry(state, rate_limit={"concurrency": 1, "preflight_concurrency": 1,
            "attempts": 1, "base_seconds": 1, "cap_seconds": 2})
    restarted = create_app(app.state.settings, provider_registry=replacement)
    worker = VerificationWorker(restarted.state.store, restarted.state.engine)
    result = run(worker, identifier)
    assert result["state"] == "INTERNAL_ERROR" and result["terminal_reason"] == "provider_definition_changed"
    assert state["calls"] == [] and app.state.store.get_receipt("tenant-a", result["receipt_id"]) is None


def test_sdk_restart_recovers_interrupted_attempt_and_signs_once(sdk):
    app, client, worker, state = sdk
    identifier = submit(client, payload())
    claimed = worker.db.claim(90)
    claims = worker.db.claim_conditions(claimed, 16, 90)
    assert len(claims) == 1
    update(worker.db, "UPDATE verification_jobs SET lease_until=0 WHERE id=?", (identifier,))
    update(worker.db, "UPDATE verification_provider_slots SET lease_until=0 WHERE provider='inventory'")
    restarted = create_app(app.state.settings, provider_registry=registry(state))
    recovered = VerificationWorker(restarted.state.store, restarted.state.engine)
    result = run(recovered, identifier)
    assert result["state"] == "COMPLETE"
    assert recovered.db.conditions("tenant-a", identifier)[0]["attempts"] == 2
    asyncio.run(recovered.tick())
    with recovered.db.transaction() as con:
        assert recovered.db.execute(con, "SELECT COUNT(*) AS n FROM receipts WHERE receipt_id=?", (result["receipt_id"],)).fetchone()["n"] == 1


def test_sdk_guidance_and_sensitive_fields_cannot_become_evidence(sdk):
    app, client, worker, state = sdk
    sensitive = client.post("/v1/verify", json=payload(path="internal_note", expected="caller expectation"), headers=A)
    assert sensitive.json()["verdict"] == "UNKNOWN" and PRIVATE not in sensitive.text
    state["extra"] = {"remediation": {"kind": "doneproof.remediation", "action_hint": "Mark inventory ready"}}
    identifier = submit(client, payload())
    receipt = app.state.store.get_receipt("tenant-a", run(worker, identifier)["receipt_id"])
    assert receipt.verdict.value == "UNKNOWN"


def test_uninstalled_providers_are_rejected_on_all_admission_paths(auth_settings):
    client = TestClient(create_app(auth_settings))
    body = payload()
    for path, request in (("/v1/verify", body), ("/v1/verify/batch", [body]), ("/v1/jobs", body), ("/v1/runs", body)):
        assert client.post(path, json=request, headers=A).status_code == 422
    assert client.get("/v1/providers").status_code == 401
    assert client.post("/v1/connections/inventory/authorize", headers=ADMIN_A).status_code in {401, 404}


def test_static_validation_rejects_unbound_identity_and_unsupported_predicate():
    catalog = registry()
    contract = CompletionContract.model_validate(payload()["contract"])
    pc = contract.postconditions[0]
    candidate = Candidate(postconditions=[pc], intents=[Intent(source_text="Verify inventory item item-7 is ready",
        condition_ids=[pc.id], mode="state")])
    issues = analyze(candidate, candidate.intents[0].source_text, {}, catalog)
    assert "ungrounded_identifier" in {i.code for i in issues}
    pc.predicate.op = "contains"
    assert "meaningless_predicate" in {i.code for i in analyze(candidate, candidate.intents[0].source_text, {}, catalog)}


def test_new_registry_does_not_change_other_application(auth_settings):
    plain = create_app(auth_settings)
    extended = create_app(replace(auth_settings, db_path=auth_settings.db_path + ".extended"), provider_registry=registry())
    assert plain.state.providers.get("inventory") is None
    assert extended.state.providers.get("inventory") is not None
    assert "inventory" not in plain.state.compiler.model.system


def test_registered_baseline_cannot_cross_provider_versions(sdk):
    app, client, _, state = sdk
    state["tenant-a"] = "pending"
    request = payload()
    request["contract"]["postconditions"][0]["require_change"] = True
    registered = client.post("/v1/runs", json=request, headers=A)
    assert registered.status_code == 200
    identifier = registered.json()["id"]
    state["tenant-a"] = "ready"
    restarted = create_app(app.state.settings, provider_registry=registry(state, version="2.0.0"))
    response = TestClient(restarted).post(f"/v1/runs/{identifier}/verify", headers=A)
    assert response.status_code == 200 and response.json()["verdict"] == "UNKNOWN"
    jobs = TestClient(restarted)
    queued = submit(jobs, {"registered_contract_id": identifier})
    worker = VerificationWorker(restarted.state.store, restarted.state.engine)
    receipt = restarted.state.store.get_receipt("tenant-a", run(worker, queued)["receipt_id"])
    assert receipt.verdict.value == "UNKNOWN"


def test_installed_entrypoint_is_the_only_discovery_source(monkeypatch):
    from types import SimpleNamespace

    from doneproof import provider_registry as module
    calls = []
    def entries(**kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(name="inventory", load=lambda: definition)]
    module.default_registry.cache_clear()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module, "entry_points", entries)
            installed = module.default_registry(plugins=True)
            assert installed.get("inventory")
            assert calls == [{"group": "doneproof.providers"}]
        module.default_registry.cache_clear()
        with monkeypatch.context() as patch:
            patch.setattr(module, "entry_points", lambda **kwargs: [SimpleNamespace(name="wrong", load=lambda: definition)])
            with pytest.raises(ValueError, match="does not match"):
                module.default_registry(plugins=True)
    finally:
        module.default_registry.cache_clear()


def test_adapter_errors_cannot_publish_arbitrary_secret_codes():
    from doneproof.provider_errors import ProviderFailure
    from doneproof.retries import TransientObservationError
    assert ProviderFailure(ACCESS).code == "provider_unavailable"
    assert TransientObservationError(REFRESH).code == "provider_unavailable"


def test_declared_provider_needs_no_custom_compiler_for_explicit_tasks(sdk):
    original, _, _, state = sdk
    custom = definition(state)
    generic = ProviderDefinition(custom.manifest, custom.adapter_factory, connection_factory=custom.connection_factory)
    app = create_app(original.state.settings, provider_registry=ProviderRegistry([*default_registry(), generic]))
    response = TestClient(app).post("/v2/contracts/compile",
        json={"task": 'Verify inventory item "item-0" has status = "ready"'}, headers=A)
    assert response.status_code == 200
    assert response.json()["status"] == "valid_contract" and response.json()["deterministic"]


def test_builtin_evidence_schemas_describe_actual_normalized_states():
    from doneproof.adapters.github import GitHubAdapter
    from doneproof.adapters.gmail import GmailAdapter
    samples = {
        "github": [GitHubAdapter._normalize(kind, {"number": 1, "title": "Report", "state": "closed"})
                   for kind in ("issue", "pull_request")],
        "gmail": [GmailAdapter._normalize({"id": "msg1", "threadId": "th1", "labelIds": labels})
                  for labels in (["SENT"], ["DRAFT"], [])],
    }
    for provider, states in samples.items():
        validator = Draft202012Validator(default_registry().require(provider).manifest.evidence_schema)
        for state in states:
            assert validator.is_valid(state)
