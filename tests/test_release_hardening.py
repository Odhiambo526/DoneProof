import asyncio
import json
import threading
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from doneproof.app import create_app
from doneproof.http import bounded_request
from doneproof.worker import VerificationWorker
from doneproof.worker_health import WorkerHealth, healthy
from tests.browser_helpers import app_for
from tests.browser_helpers import payload as browser_payload
from tests.test_jobs import A, jobs, submit  # noqa: F401


def test_readiness_is_uncached_api_scope_and_database_failure_is_sanitized(auth_settings, monkeypatch):
    app = create_app(auth_settings)
    client = TestClient(app)
    response = client.get("/ready")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["workers"] == "not_observed" and response.json()["system_operational"] is None
    assert response.json()["schema_version"] == 7
    def fail():
        raise RuntimeError("postgresql://secret-sentinel")
    monkeypatch.setattr(app.state.store, "ping", fail)
    response = client.get("/ready")
    assert response.status_code == 503 and "secret-sentinel" not in response.text


def test_staging_enforces_production_boundaries(settings):
    with pytest.raises(RuntimeError, match="API_KEYS"):
        create_app(replace(settings, env="staging"))


def test_chunked_body_bound_and_caller_text_absent_from_logs(auth_settings, caplog):
    app = create_app(replace(auth_settings, max_body_bytes=1024))
    client = TestClient(app)
    with caplog.at_level("INFO", logger="doneproof"):
        response = client.post("/v2/contracts/compile", headers={"X-DoneProof-Key": "key-a"},
                               content=iter([b'{"task":"', b"secret-sentinel" * 100, b'"}']))
    assert response.status_code == 413
    assert "secret-sentinel" not in caplog.text and "key-a" not in caplog.text


def test_settings_repr_cannot_export_credentials(connection_settings):
    configured = replace(connection_settings, database_url="secret-dsn-sentinel", openai_api_key="model-secret-sentinel")
    value = repr(configured)
    for secret in ("secret-dsn-sentinel", "model-secret-sentinel", "google-secret-sentinel", "key-a", "admin-a"):
        assert secret not in value


@pytest.mark.parametrize("kind", ["declared", "streamed", "encoded"])
def test_response_bounds_before_parsing(kind):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 33
    headers = {"content-length": "33"} if kind == "declared" else {"content-encoding": "gzip"} if kind == "encoded" else {}
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, headers=headers, stream=Stream()))) as client:
            with pytest.raises(httpx.DecodingError):
                await bounded_request(client, "GET", "https://provider.example/resource", max_bytes=32)
    asyncio.run(run())


def test_worker_database_wait_does_not_block_event_loop(jobs, monkeypatch):  # noqa: F811
    worker = jobs[2]
    entered, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3), "Queue I/O blocked the event loop"
    monkeypatch.setattr(worker.db, "claim", blocked)
    async def run():
        task = asyncio.create_task(worker.tick())
        assert await asyncio.to_thread(entered.wait, 2)
        release.set()
        assert await task is False
    asyncio.run(run())


def test_worker_partitions_whole_browser_jobs(connection_settings):
    app, client, _, _ = app_for(connection_settings)
    identifier = submit(client, browser_payload(), A)
    regular = VerificationWorker(app.state.store, app.state.engine, exclude_providers=("browser",))
    browser = VerificationWorker(app.state.store, app.state.engine, require_providers=("browser",))
    assert asyncio.run(regular.tick()) is False
    assert regular.db.get_job("tenant-a", identifier)["state"] == "QUEUED"
    assert asyncio.run(browser.tick()) is True
    with pytest.raises(ValueError):
        VerificationWorker(app.state.store, app.state.engine, require_providers=("not_installed",))


def test_worker_local_health_needs_every_loop_and_expires(tmp_path):
    path = tmp_path / "worker-health.json"
    health = WorkerHealth(path)
    assert not healthy(path)
    health.update("tick")
    assert not healthy(path)
    health.update("callback_tick")
    health.update("recovery_tick")
    assert healthy(path)
    now = max(json.loads(path.read_text())["loops"].values()) + 121
    assert not healthy(path, now)
    health.remove()
    assert not path.exists()
