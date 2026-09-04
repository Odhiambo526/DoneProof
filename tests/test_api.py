from dataclasses import replace

from fastapi.testclient import TestClient

from doneproof.app import create_app
from tests.fakes import MockAdapter


def make_app(settings):
    return create_app(settings, adapter_overrides={"unresolved": MockAdapter()})


def sample_contract():
    return {
        "contract": {
            "task": "Create and assign issue",
            "postconditions": [
                {
                    "id": "p1",
                    "description": "created",
                    "provider": "unresolved",
                    "selector": {"state": {"created": True}},
                    "predicate": {"op": "eq", "path": "created", "expected": True},
                    "required": True,
                },
                {
                    "id": "p2",
                    "description": "assigned",
                    "provider": "unresolved",
                    "selector": {"state": {"assignees": []}},
                    "predicate": {"op": "contains", "path": "assignees", "expected": "alice"},
                    "required": True,
                },
            ],
        }
    }


def test_health_and_customer_surfaces(settings):
    client = TestClient(make_app(settings))
    assert client.get("/health").json()["ok"] is True
    assert "Agents act" in client.get("/").text
    assert "Outcome assurance" in client.get("/console").text


def test_verify_receipt_integrity_and_certificate(settings):
    client = TestClient(make_app(settings))
    r = client.post("/v1/verify", json=sample_contract())
    assert r.status_code == 200 and r.json()["verdict"] == "PARTIAL"
    rid = r.json()["receipt_id"]
    integrity = client.get(f"/v1/receipts/{rid}/integrity").json()
    assert integrity["valid"] is True
    assert integrity["verification_scope"] == "integrity_only"
    cert = client.get(f"/v1/receipts/{rid}/certificate")
    assert cert.status_code == 200 and rid in cert.text


def test_idempotency_returns_same_receipt(settings):
    client = TestClient(make_app(settings))
    h = {"Idempotency-Key": "run-123"}
    a = client.post("/v1/verify", json=sample_contract(), headers=h)
    b = client.post("/v1/verify", json=sample_contract(), headers=h)
    assert a.json()["receipt_id"] == b.json()["receipt_id"]
    assert client.get("/v1/overview").json()["total"] == 1


def test_idempotency_conflict(settings):
    client = TestClient(make_app(settings))
    h = {"Idempotency-Key": "same"}
    assert client.post("/v1/verify", json=sample_contract(), headers=h).status_code == 200
    other = sample_contract()
    other["contract"]["task"] = "Different task"
    assert client.post("/v1/verify", json=other, headers=h).status_code == 409


def test_tenant_receipt_isolation(auth_settings):
    app = make_app(auth_settings)
    client = TestClient(app)
    assert client.post("/v1/verify", json=sample_contract(), headers={"X-DoneProof-Key": "key-a"}).status_code == 200
    assert len(client.get("/v1/receipts", headers={"X-DoneProof-Key": "key-a"}).json()) == 1
    assert len(client.get("/v1/receipts", headers={"X-DoneProof-Key": "key-b"}).json()) == 0
    assert client.get("/v1/receipts").status_code == 401


def test_production_fails_closed_without_required_controls(settings):
    import pytest

    prod = replace(
        settings, env="production", api_keys={}, signing_seed_b64=None, legacy_receipt_key="dev-only-change-me"
    )
    with pytest.raises(RuntimeError, match="API_KEYS"):
        create_app(prod)


def test_production_requires_durable_database_after_auth_and_signing(settings):
    import pytest

    prod = replace(settings, env="production", api_keys={"prod-key": "acme"})
    with pytest.raises(RuntimeError, match="durable PostgreSQL"):
        create_app(prod, adapter_overrides={"unresolved": MockAdapter()})


def test_demo_endpoint_is_not_part_of_product_runtime(settings):
    assert TestClient(make_app(settings)).post("/v1/demo/verify").status_code == 404


def test_registered_run_gets_server_time_and_registered_assurance(settings):
    client = TestClient(make_app(settings))
    payload = sample_contract()
    payload["contract"]["task_started_at"] = "2000-01-01T00:00:00Z"
    reg = client.post("/v1/runs", json=payload)
    assert reg.status_code == 200
    assert not reg.json()["task_started_at"].startswith("2000-01-01")
    rid = reg.json()["id"]
    verified = client.post(f"/v1/runs/{rid}/verify")
    assert verified.status_code == 200
    assert verified.json()["assurance_level"] == "registered"


def test_submitted_verify_is_marked_lower_assurance(settings):
    client = TestClient(make_app(settings))
    r = client.post("/v1/verify", json=sample_contract())
    assert r.json()["assurance_level"] == "submitted"


def test_signing_key_is_public_even_when_workspace_auth_is_enabled(auth_settings):
    client = TestClient(make_app(auth_settings))
    r = client.get("/v1/signing-key")
    assert r.status_code == 200
    assert r.json()["algorithm"] == "Ed25519"
    assert "Pin this public key" in r.json()["trust_model"]


def test_request_body_limit_is_enforced(settings):
    tiny = replace(settings, max_body_bytes=1024)
    client = TestClient(make_app(tiny))
    r = client.post(
        "/v1/verify", content=b"{" + b'"x":"' + b"a" * 2000 + b'"}', headers={"content-type": "application/json"}
    )
    assert r.status_code == 413


def test_portable_receipt_bundle_contains_verifiable_material(settings):
    client = TestClient(make_app(settings))
    r = client.post("/v1/verify", json=sample_contract())
    rid = r.json()["receipt_id"]
    bundle = client.get(f"/v1/receipts/{rid}/bundle")
    assert bundle.status_code == 200
    body = bundle.json()
    assert body["schema"] == "doneproof-evidence-bundle/v1"
    assert body["integrity"]["valid"] is True
    assert body["receipt"]["receipt_id"] == rid
    assert body["signing_key"]["algorithm"] == "Ed25519"
    assert body["integrity"]["scope"] == "integrity_only"
    assert body["trust"]["issuer_authenticity"] == "requires_pinned_public_key"


def test_batch_verification_and_limit(settings):
    client = TestClient(make_app(settings))
    payload = [sample_contract(), sample_contract()]
    payload[1]["contract"]["task"] = "Second outcome"
    r = client.post("/v1/verify/batch", json=payload)
    assert r.status_code == 200 and len(r.json()) == 2
    assert all(x["verdict"] == "PARTIAL" for x in r.json())
    limited = replace(settings, max_batch_size=1)
    assert TestClient(make_app(limited)).post("/v1/verify/batch", json=payload).status_code == 413


def test_audit_trail_records_customer_relevant_actions(settings):
    client = TestClient(make_app(settings))
    r = client.post("/v1/verify", json=sample_contract())
    events = client.get("/v1/audit").json()
    assert any(x["action"] == "verification.completed" and x["object_id"] == r.json()["receipt_id"] for x in events)
    event = next(x for x in events if x["object_id"] == r.json()["receipt_id"])
    assert event["metadata"]["verdict"] == "PARTIAL"


def test_rate_limit_returns_retry_after(settings):
    limited = replace(settings, requests_per_minute=2)
    client = TestClient(make_app(limited))
    assert client.get("/v1/overview").status_code == 200
    assert client.get("/v1/overview").status_code == 200
    r = client.get("/v1/overview")
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


def test_customer_responses_have_security_and_latency_headers(settings):
    r = TestClient(make_app(settings)).get("/")
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert float(r.headers["X-DoneProof-Duration-Ms"]) >= 0


def test_receipt_binds_exact_completion_contract(settings):
    client = TestClient(make_app(settings))
    r = client.post("/v1/verify", json=sample_contract())
    assert r.status_code == 200
    assert len(r.json()["contract_hash"]) == 64


def test_receipt_integrity_survives_signing_key_rotation(settings):
    import base64
    from dataclasses import replace

    first = TestClient(make_app(settings))
    created = first.post("/v1/verify", json=sample_contract()).json()
    rid = created["receipt_id"]
    old_key = created["public_key"]

    rotated = replace(settings, signing_seed_b64=base64.b64encode(b"R" * 32).decode())
    second = TestClient(make_app(rotated))
    integrity = second.get(f"/v1/receipts/{rid}/integrity")
    bundle = second.get(f"/v1/receipts/{rid}/bundle")
    assert integrity.status_code == 200 and integrity.json()["valid"] is True
    assert bundle.json()["signing_key"]["public_key"] == old_key
    assert bundle.json()["signing_key"]["public_key"] != second.get("/v1/signing-key").json()["public_key"]


def test_contract_ids_are_immutable_within_workspace(settings):
    client = TestClient(make_app(settings))
    payload = sample_contract()
    payload["contract"]["id"] = "cc_immutable"
    payload["contract"]["task_started_at"] = "2026-09-04T03:00:00Z"
    payload["contract"]["created_at"] = "2026-09-04T03:00:00Z"
    assert client.post("/v1/verify", json=payload).status_code == 200
    payload["contract"]["task"] = "Different task under same contract id"
    conflict = client.post("/v1/verify", json=payload)
    assert conflict.status_code == 409


def test_customer_openapi_hides_internal_demo_vocabulary(settings):
    schema = create_app(settings).openapi()
    rendered = str(schema)
    postcondition = schema["components"]["schemas"]["Postcondition"]
    receipt = schema["components"]["schemas"]["VerificationReceipt"]
    assert "mock" not in postcondition["properties"]["provider"]["enum"]
    assert "synthetic" not in receipt["properties"]["assurance_level"]["enum"]
    assert "/v1/demo/verify" not in schema["paths"]
    assert "MockAdapter" not in rendered


def test_console_does_not_persist_api_key_across_browser_sessions(settings):
    page = TestClient(make_app(settings)).get("/console").text
    assert "sessionStorage" in page
    assert "localStorage" not in page


def test_untrusted_request_id_is_not_reflected_verbatim(settings):
    client = TestClient(make_app(settings))
    r = client.get("/health", headers={"X-Request-ID": "bad request id with spaces"})
    assert r.headers["X-Request-ID"].startswith("req_")
    ok = client.get("/health", headers={"X-Request-ID": "customer-req:123"})
    assert ok.headers["X-Request-ID"] == "customer-req:123"


def test_production_requires_stable_signing_key_after_auth(settings):
    import pytest

    prod = replace(
        settings,
        env="production",
        api_keys={"prod-key": "acme"},
        signing_seed_b64=None,
        legacy_receipt_key="dev-only-change-me",
    )
    with pytest.raises(RuntimeError, match="signing key"):
        create_app(prod)
