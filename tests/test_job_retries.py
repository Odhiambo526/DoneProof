from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
import pytest

from doneproof.http import resilient_get
from doneproof.retries import (
    POLICIES,
    TransientObservationError,
    durable_observation,
    retry_after_seconds,
    transient_exception,
    transient_response,
)
from tests.connection_helpers import ACCESS, seed
from tests.test_jobs import A, payload, submit


@pytest.mark.parametrize("provider", ["github", "gmail", "webhook", "unresolved"])
def test_policy_backoff_is_bounded_jittered_and_never_truncates_retry_after(provider):
    policy = POLICIES[provider]
    for attempt in range(1, 10):
        ceiling = min(policy.cap_seconds, policy.base_seconds * 2 ** (attempt - 1))
        assert policy.delay(attempt, rng=lambda: 0) == ceiling / 2
        assert policy.delay(attempt, rng=lambda: 1) == ceiling
        assert policy.delay(attempt, 900, rng=lambda: 0) == 900


def test_retry_after_seconds_dates_rate_reset_and_invalid_values():
    now = 1700000000
    assert retry_after_seconds({"Retry-After": "12"}, now) == 12
    date = format_datetime(datetime.fromtimestamp(now + 45, timezone.utc))
    assert retry_after_seconds({"Retry-After": date}, now) == 45
    assert retry_after_seconds({"Retry-After": "1", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(now + 90)}, now) == 90
    for value in ("NaN", "Infinity", "-1", "bad-date"):
        assert retry_after_seconds({"Retry-After": value}, now) == 0


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422, 501, 505])
def test_semantic_http_errors_are_never_transient(code):
    response = httpx.Response(code, json={"error": {"errors": [{"reason": "forbidden"}]}},
                              request=httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages/1"))
    assert transient_response(response) is None


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_infrastructure_statuses_are_transient(code):
    response = httpx.Response(code, headers={"Retry-After": "90"})
    assert transient_response(response).retry_after == 90


def test_provider_specific_quota_errors():
    github = httpx.Response(403, headers={"Retry-After": "3"}, request=httpx.Request("GET", "https://api.github.com/user"))
    assert transient_response(github).retry_after >= 60
    gmail = httpx.Response(403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}},
                           request=httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile"))
    assert transient_response(gmail)
    assert transient_response(github, provider_errors=False) is None
    assert transient_exception(httpx.ReadTimeout("secret-sentinel"))
    assert transient_exception(httpx.ConnectError("secret-sentinel"))
    assert not transient_exception(ValueError("invalid selector"))


def test_durable_http_performs_one_attempt_without_sleeping():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "900"})
    async def request():
        token = durable_observation.set(True)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                with pytest.raises(TransientObservationError) as failure:
                    await resilient_get(client, "https://api.github.com/user")
                assert failure.value.retry_after == 900
        finally:
            durable_observation.reset(token)
    asyncio.run(request())
    assert len(calls) == 1


def test_managed_provider_throttle_keeps_connection_and_retries(connection_app):
    from fastapi.testclient import TestClient

    from doneproof.worker import VerificationWorker
    app, stub = connection_app
    row = seed(app.state.connections)
    def handle(request):
        return httpx.Response(429, headers={"Retry-After": "90"}, json={"error": "upstream-secret-sentinel"})
    app.state.connections.providers.transport = httpx.MockTransport(handle)
    client = TestClient(app)
    body = payload(provider="gmail")
    body["contract"]["postconditions"][0]["selector"] = {"message_id": "msg1"}
    identifier = submit(client, body)
    worker = VerificationWorker(app.state.store, app.state.engine)
    asyncio.run(worker.tick())
    current = app.state.connections.db.get("tenant-a", provider="gmail")
    assert current["state"] == "connected" and current["revision"] == row["revision"]
    records = client.get(f"/v1/jobs/{identifier}/conditions", headers=A)
    assert records.json()["conditions"][0]["state"] == "PENDING"
    assert "upstream-secret-sentinel" not in records.text and ACCESS not in records.text


def test_interrupted_refresh_is_not_retried_as_an_observation(connection_app):
    from fastapi.testclient import TestClient

    from doneproof.worker import VerificationWorker
    app, _ = connection_app
    row = seed(app.state.connections)
    app.state.connections.db.update(row, expires_at=int(time.time()) - 1)
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(429, json={"error": "provider quota"})
    app.state.connections.providers.transport = httpx.MockTransport(handle)
    client = TestClient(app)
    body = payload(provider="gmail")
    body["contract"]["postconditions"][0]["selector"] = {"message_id": "msg1"}
    identifier = submit(client, body)
    worker = VerificationWorker(app.state.store, app.state.engine)
    result = asyncio.run(worker.run_until_terminal("tenant-a", identifier))
    assert app.state.connections.db.get("tenant-a", provider="gmail")["state"] == "reconnect_required"
    assert result["state"] == "COMPLETE" and len(requests) == 1
    assert app.state.store.get_receipt("tenant-a", result["receipt_id"]).verdict.value == "UNKNOWN"


def test_disconnect_between_observation_and_signing_invalidates_checkpoint(connection_app):
    from fastapi.testclient import TestClient

    from doneproof.worker import VerificationWorker
    app, _ = connection_app
    row = seed(app.state.connections)
    client = TestClient(app)
    body = payload(provider="gmail")
    pc = body["contract"]["postconditions"][0]
    pc.update(selector={"message_id": "msg1"}, predicate={"op": "eq", "path": "sent", "expected": True})
    identifier = submit(client, body)
    worker = VerificationWorker(app.state.store, app.state.engine)
    asyncio.run(worker.tick())
    asyncio.run(worker.tick())
    assert worker.db.get_job("tenant-a", identifier)["state"] == "SIGNING"
    app.state.connections.db.disable(row)
    asyncio.run(worker.tick())
    result = worker.db.get_job("tenant-a", identifier)
    receipt = app.state.store.get_receipt("tenant-a", result["receipt_id"])
    assert receipt.verdict.value == "UNKNOWN"
    assert "changed before receipt" in receipt.results[0].reason
