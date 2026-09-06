import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from doneproof.callback_receiver import create_receiver


def test_receiver_durable_signed_delivery_and_duplicate(connection_settings):
    if not connection_settings.database_url:
        # The staging receiver deliberately refuses a non-durable backend.
        import pytest
        with pytest.raises(RuntimeError):
            create_receiver("sqlite:///local.db", {"rc": "s" * 32})
        return
    secret = "s" * 32
    app = create_receiver(connection_settings.database_url, {"rc": secret})
    client = TestClient(app)
    body = json.dumps({"event_id": "ve_" + "a" * 32, "job_id": "vj_" + "a" * 32, "state": "COMPLETE",
                      "receipt_id": "vr_" + "a" * 32, "finished_at": time.time()}).encode()
    stamp = str(int(time.time()))
    headers = {"X-DoneProof-Timestamp": stamp, "X-DoneProof-Event": "ve_" + "a" * 32,
               "X-DoneProof-Signature": "sha256=" + hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()}
    assert client.post("/callbacks/rc", content=body, headers=headers).status_code == 200
    # A new process/application instance uses the durable inbox, not memory.
    restarted = TestClient(create_receiver(connection_settings.database_url, {"rc": secret}))
    assert restarted.post("/callbacks/rc", content=body, headers=headers).status_code == 200
    assert restarted.post("/callbacks/rc", content=body + b"x", headers=headers).status_code == 400
    assert restarted.post("/callbacks/other", content=body, headers=headers).status_code == 404
    assert restarted.post("/callbacks/rc", content=b"x" * 16385, headers=headers).status_code == 413
    import psycopg
    with psycopg.connect(connection_settings.database_url) as con:
        assert con.execute("SELECT COUNT(*) FROM rc_callback_inbox").fetchone()[0] == 1
