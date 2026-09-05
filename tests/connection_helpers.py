from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx

from doneproof.connection_providers import GMAIL_SCOPE

ADMIN_A = {"X-DoneProof-Key": "admin-a"}
ADMIN_B = {"X-DoneProof-Key": "admin-b"}
ACCESS = "test-access-sentinel"
REFRESH = "test-refresh-sentinel"


class ProviderStub:
    def __init__(self):
        self.requests = []
        self.email = "partner@example.test"
        self.github_id = 123
        self.status = 200
        self.revoke_status = 200
        self.refresh_calls = 0
        self.rotated = "test-rotated-sentinel"
        self.token_data = {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600,
                           "scope": GMAIL_SCOPE, "token_type": "Bearer"}
        self.installations = {"total_count": 1, "installations": [
            {"permissions": {"issues": "read", "pull_requests": "read", "metadata": "read"}}]}
        self.message_status = 200
        self.labels = ["SENT"]
        self.pause_refresh = None
        self.pause_observe = None

    async def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/revoke") or path.endswith("/grant"):
            return httpx.Response(self.revoke_status, json={})
        if path in {"/token", "/login/oauth/access_token"}:
            args = parse_qs(request.content.decode())
            if args.get("grant_type") == ["refresh_token"]:
                self.refresh_calls += 1
                if self.pause_refresh:
                    await self.pause_refresh()
                data = {**self.token_data, "access_token": self.rotated, "refresh_token": "test-next-refresh"}
                if request.url.host == "github.com":
                    data["scope"] = ""
                    data["refresh_token_expires_in"] = 86400
                return httpx.Response(self.status, json=data if self.status == 200 else {"error": "invalid_grant"})
            data = dict(self.token_data)
            if request.url.host == "github.com":
                data["scope"] = ""
                data["refresh_token_expires_in"] = 86400
            return httpx.Response(self.status, json=data)
        if path.endswith("/profile"):
            return httpx.Response(self.status, json={"emailAddress": self.email})
        if path == "/user":
            return httpx.Response(self.status, json={"id": self.github_id, "login": "design-partner"})
        if path == "/user/installations":
            return httpx.Response(self.status, json=self.installations)
        if "/messages/" in path:
            if self.pause_observe:
                await self.pause_observe()
            return httpx.Response(self.message_status, json={"id": "msg1", "threadId": "th1",
                "labelIds": self.labels, "internalDate": "1770000000000",
                "payload": {"headers": [{"name": "To", "value": "recipient@example.test"},
                                       {"name": "Subject", "value": "Report"}], "parts": []}})
        if path.startswith("/repos/"):
            return httpx.Response(self.message_status, json={"number": 1, "state": "closed",
                "title": "Report", "html_url": "https://github.com/example/project/issues/1"})
        raise AssertionError("Unexpected provider endpoint: " + str(request.url))

    def attach(self, service):
        service.providers.transport = httpx.MockTransport(self)


def seed(service, provider="gmail", tenant="tenant-a", *, expires_in=3600, kind="oauth"):
    row = service.db.ensure(tenant, provider)
    data = {"access_token": ACCESS, "refresh_token": REFRESH,
            "expires_at": int(time.time()) + expires_in, "refresh_expires_at": int(time.time()) + 86400,
            "scopes": [GMAIL_SCOPE] if provider == "gmail" else [], "kind": kind}
    return service.save_credentials(row, data, "partner@example.test" if provider == "gmail" else "123",
                                    "partner@example.test" if provider == "gmail" else "design-partner")


def begin(client, provider="gmail", headers=None):
    response = client.post(f"/v1/connections/{provider}/authorize", headers=headers or ADMIN_A)
    assert response.status_code == 200
    url = response.json()["authorization_url"]
    return parse_qs(urlsplit(url).query), response


def finish(client, query, provider="gmail", **extra):
    return client.get(f"/v1/connections/oauth/{provider}/callback",
                      params={"state": query["state"][0], "code": "test-code-sentinel", **extra},
                      follow_redirects=False)


def run(coro):
    return asyncio.run(coro)


def body(request):
    return json.loads(request.content) if request.headers.get("content-type", "").startswith("application/json") else parse_qs(request.content.decode())
