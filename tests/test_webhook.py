import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from doneproof.app import create_app


def sign(secret, ts, event, object_id, raw):
    base = f"{ts}.{event}.{object_id}.".encode() + raw
    return "sha256=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def test_signed_webhook_can_verify_enterprise_outcome(webhook_settings):
    client = TestClient(create_app(webhook_settings))
    ts = int(time.time())
    event = "refund.completed"
    oid = "refund-42"
    raw = json.dumps({"status": "completed", "amount": 1200}, separators=(",", ":")).encode()
    headers = {
        "content-type": "application/json",
        "X-DoneProof-Timestamp": str(ts),
        "X-DoneProof-Event": event,
        "X-DoneProof-Object-ID": oid,
        "X-DoneProof-Signature": sign("whsec_test", ts, event, oid, raw),
    }
    ing = client.post("/v1/webhooks/erp", content=raw, headers=headers)
    assert ing.status_code == 200 and ing.json()["accepted"] is True
    contract = {
        "contract": {
            "task": "Complete refund",
            "task_started_at": ts - 1,
            "postconditions": [
                {
                    "id": "p1",
                    "description": "refund completed",
                    "provider": "webhook",
                    "selector": {"source": "erp", "event_type": event, "object_id": oid},
                    "predicate": {"op": "eq", "path": "payload.status", "expected": "completed"},
                    "required": True,
                }
            ],
        }
    }
    r = client.post("/v1/verify", json=contract)
    assert r.status_code == 200 and r.json()["verdict"] == "VERIFIED"


def test_webhook_replay_is_deduplicated(webhook_settings):
    client = TestClient(create_app(webhook_settings))
    ts = int(time.time())
    raw = b'{"status":"ok"}'
    event = "job.done"
    oid = "j1"
    h = {
        "content-type": "application/json",
        "X-DoneProof-Timestamp": str(ts),
        "X-DoneProof-Event": event,
        "X-DoneProof-Object-ID": oid,
        "X-DoneProof-Signature": sign("whsec_test", ts, event, oid, raw),
    }
    assert client.post("/v1/webhooks/erp", content=raw, headers=h).json()["duplicate"] is False
    assert client.post("/v1/webhooks/erp", content=raw, headers=h).json()["duplicate"] is True


def test_webhook_rejects_bad_signature_and_stale_timestamp(webhook_settings):
    client = TestClient(create_app(webhook_settings))
    raw = b"{}"
    h = {
        "content-type": "application/json",
        "X-DoneProof-Timestamp": str(int(time.time())),
        "X-DoneProof-Event": "x",
        "X-DoneProof-Object-ID": "1",
        "X-DoneProof-Signature": "sha256=deadbeef",
    }
    assert client.post("/v1/webhooks/erp", content=raw, headers=h).status_code == 401
    old = int(time.time()) - 10000
    h["X-DoneProof-Timestamp"] = str(old)
    h["X-DoneProof-Signature"] = sign("whsec_test", old, "x", "1", raw)
    assert client.post("/v1/webhooks/erp", content=raw, headers=h).status_code == 401
