from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from doneproof.adapters.base import ObservationContext
from doneproof.adapters.github import GitHubAdapter
from doneproof.adapters.gmail import GmailAdapter

CONTEXT = ObservationContext("default", "cc_preflight", "2026-01-01T00:00:00+00:00")


def test_github_page_budget_never_proves_absence_or_uniqueness():
    calls = []
    async def provider(request):
        calls.append(request)
        return httpx.Response(200, json=[{"number": n + 1, "title": "Other", "created_at": "2026-02-01T00:00:00Z"}
                                        for n in range(100)])
    adapter = GitHubAdapter(allow_env=False, transport=httpx.MockTransport(provider))
    result = asyncio.run(adapter.observe({"repo": "acme/api", "kind": "issue", "title": "Missing",
                                         "created_after": CONTEXT.task_started_at}, CONTEXT))
    assert result.indeterminate
    assert result.state is None
    assert len(calls) == 5


@pytest.mark.parametrize("response", [
    {"messages": [{"id": "msg1"}], "nextPageToken": "page2"},
    {"messages": [{}]},
    {"messages": [{"id": "msg1"}]},
])
def test_gmail_incomplete_search_is_unknown(settings, response):
    async def provider(request):
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json=response)
        return httpx.Response(404, json={})
    adapter = GmailAdapter(replace(settings, gmail_tokens={"default": "test-token"}),
                           transport=httpx.MockTransport(provider))
    result = asyncio.run(adapter.observe({"subject": "Report", "to": "ana@example.com"}, CONTEXT))
    assert result.indeterminate
    assert result.state is None
