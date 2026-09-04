import os
os.environ["DONEPROOF_DB"] = "/tmp/doneproof-test.db"

from fastapi.testclient import TestClient
from doneproof.app import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_demo_exposes_partial_completion():
    r = client.post("/v1/verify/demo")
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "PARTIAL"
    assert [x["status"] for x in body["results"]] == ["PASS", "FAIL"]
