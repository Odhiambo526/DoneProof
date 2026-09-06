"""Real loopback HTTP and separate persistent worker processes; controlled signed events."""
import hashlib
import hmac
import json
import math
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from doneproof import DoneProof
from doneproof.signing import ReceiptSigner


def test_http_sdk_persistent_worker_restart_and_receipt_chain(connection_settings, tmp_path):
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(
        ("DONEPROOF_", "OPENAI_", "GITHUB_", "GMAIL_", "DATABASE_URL", "POSTGRES_URL"))}
    env.update(DONEPROOF_ENV="test", DONEPROOF_DB=connection_settings.db_path,
               DONEPROOF_API_KEYS_JSON=json.dumps({"key-a": "tenant-a", "key-b": "tenant-b"}),
               DONEPROOF_SIGNING_SEED_B64=connection_settings.signing_seed_b64,
               DONEPROOF_WEBHOOK_SOURCES_JSON=json.dumps({"erp": {"tenant": "tenant-a", "secret": "controlled-source-secret"}}),
               DONEPROOF_WORKER_HEALTH_FILE=str(tmp_path / "worker-health.json"))
    if connection_settings.database_url:
        env["DATABASE_URL"] = connection_settings.database_url
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    def launch(args):
        return subprocess.Popen([sys.executable, *args], cwd=root, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)
    api = launch(["-m", "uvicorn", "doneproof.app:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"])
    worker = None
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 45
        while True:
            try:
                if httpx.get(base + "/ready", timeout=2, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline and api.poll() is None, "Controlled API did not become ready"
            time.sleep(.1)
        def event(state, timestamp):
            raw = json.dumps({"status": state}).encode()
            signed = f"{timestamp}.order.updated.order-rc.".encode() + raw
            headers = {"X-DoneProof-Timestamp": str(timestamp), "X-DoneProof-Event": "order.updated",
                       "X-DoneProof-Object-ID": "order-rc",
                       "X-DoneProof-Signature": hmac.new(b"controlled-source-secret", signed, hashlib.sha256).hexdigest()}
            assert httpx.post(base + "/v1/webhooks/erp", content=raw, headers=headers, trust_env=False).status_code == 200
        with DoneProof(api_key="key-a", base_url=base) as client:
            task = 'Wait for webhook "order.updated" from "erp" for object "order-rc" with payload.status = "sent"'
            session = client.assurance.prepare(task=task, idempotency_key="process-prepare")
            assert session.state == "READY_FOR_EXECUTION"
            assert client.assurance.prepare(task=task, idempotency_key="process-prepare").id == session.id
            stamp = math.ceil(time.time())
            event("draft", stamp)
            submitted = client.assurance.verify(session.id)
            assert client.assurance.verify(session.id).current_job_id == submitted.current_job_id
            worker = launch(["-m", "doneproof.worker"])
            failed = client.assurance.wait_for_verification(session.id, timeout=60)
            assert failed.verdict in {"FAILED", "PARTIAL"} and failed.remediation
            # Abrupt process death between lifecycle attempts; API and database survive.
            worker.kill()
            worker.wait(timeout=15)
            event("sent", stamp + 1)
            next_job = client.assurance.reverify(session.id, previous_receipt_id=failed.receipt.receipt_id,
                                                idempotency_key="process-repair")
            worker = launch(["-m", "doneproof.worker"])
            completed = client.assurance.wait_for_verification(session.id, timeout=60)
            assert completed.current_job_id == next_job.current_job_id
            assert completed.verdict == "VERIFIED" and len(completed.lineage) == 2
            assert completed.receipt.previous_receipt_id == failed.receipt.receipt_id
            assert ReceiptSigner.verify_trusted(completed.receipt, ReceiptSigner(connection_settings).public_key_b64)
        assert httpx.get(base + "/v1/assurance/sessions/" + session.id,
                         headers={"X-DoneProof-Key": "key-b"}, trust_env=False).status_code == 404
    finally:
        for process in (worker, api):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
