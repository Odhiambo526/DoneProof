import json

from fastapi.testclient import TestClient

from doneproof.worker import VerificationWorker
from tests.connection_helpers import ACCESS, REFRESH, seed
from tests.test_jobs import A, payload, run
from tests.test_recovery import again, original


def test_gmail_recovery_only_reads_and_never_sends_or_modifies_messages(connection_app, caplog):
    app, stub = connection_app
    seed(app.state.connections)
    client = TestClient(app)
    stub.labels = ["DRAFT"]
    body = payload(provider="gmail")
    pc = body["contract"]["postconditions"][0]
    pc["selector"] = {"message_id": "msg1"}
    pc["predicate"] = {"op": "eq", "path": "location", "expected": "sent"}
    root = original(client, body)
    assert root["remediation"][0]["code"] == "message_is_draft"
    # Only the external provider changes state; DoneProof has no repair call.
    stub.labels = ["SENT"]
    job = again(client, root["receipt_id"]).json()["id"]
    result = run(VerificationWorker(app.state.store, app.state.engine), job)
    receipt = app.state.store.get_receipt("tenant-a", result["receipt_id"])
    assert receipt.verdict == "VERIFIED"
    assert stub.requests and all(request.method == "GET" for request in stub.requests)
    public = receipt.model_dump_json() + json.dumps(app.state.recovery.history("tenant-a", root["receipt_id"])) + caplog.text
    assert ACCESS not in public and REFRESH not in public


def test_registered_recovery_retains_original_account_binding(connection_app):
    app, stub = connection_app
    seed(app.state.connections)
    client = TestClient(app)
    stub.labels = ["DRAFT"]
    body = payload(provider="gmail", change=True)
    pc = body["contract"]["postconditions"][0]
    pc["selector"] = {"message_id": "msg1"}
    pc["predicate"] = {"op": "eq", "path": "location", "expected": "sent"}
    contract = client.post("/v1/runs", headers=A, json=body).json()
    root = client.post(f"/v1/runs/{contract['id']}/verify", headers=A).json()
    row = app.state.connections.db.get("tenant-a", provider="gmail")
    app.state.connections.db.update(row, account_id="different@example.test")
    stub.labels = ["SENT"]
    job = again(client, root["receipt_id"]).json()["id"]
    result = run(VerificationWorker(app.state.store, app.state.engine), job)
    assert app.state.store.get_receipt("tenant-a", result["receipt_id"]).verdict == "UNKNOWN"
