import asyncio
from datetime import datetime, timezone

import httpx

from doneproof.adapters.github import GitHubAdapter
from doneproof.domain import CompletionContract, ConditionStatus, Verdict
from doneproof.engine import VerificationEngine
from doneproof.signing import ReceiptSigner

START = datetime(2026, 9, 4, 3, 0, 0, tzinfo=timezone.utc)


def issue(number, title, created_at="2026-09-04T03:01:00Z", author="bot", assignees=None):
    return {
        "number": number,
        "title": title,
        "body": "body",
        "state": "open",
        "locked": False,
        "user": {"login": author},
        "assignees": [{"login": x} for x in (assignees or [])],
        "labels": [],
        "created_at": created_at,
        "updated_at": created_at,
        "closed_at": None,
        "html_url": f"https://github.com/acme/api/issues/{number}",
    }


def contract(selector, predicate=None):
    return CompletionContract.model_validate(
        {
            "task": "Create issue Auth bypass",
            "task_started_at": START.isoformat(),
            "postconditions": [
                {
                    "id": "p1",
                    "description": "requested issue exists",
                    "provider": "github",
                    "selector": selector,
                    "predicate": predicate or {"op": "eq", "path": "title", "expected": "Auth bypass"},
                    "required": True,
                }
            ],
        }
    )


def run(c, handler, settings):
    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    return asyncio.run(VerificationEngine({"github": adapter}, ReceiptSigner(settings), timeout_seconds=2).verify(c))


def test_discovers_unique_issue_without_executor_number(settings):
    candidate = issue(77, "Auth bypass", assignees=["alice"])

    def handler(request):
        if request.url.path.endswith("/issues"):
            assert request.url.params["since"].startswith("2026-09-04T03:00:00")
            return httpx.Response(200, json=[candidate])
        if request.url.path.endswith("/issues/77"):
            return httpx.Response(200, json=candidate)
        raise AssertionError(request.url)

    r = run(contract({"repo": "acme/api", "kind": "issue", "number": None, "title": "Auth bypass"}), handler, settings)
    assert r.verdict == Verdict.VERIFIED


def test_duplicate_matches_are_unknown(settings):
    one, two = issue(77, "Auth bypass"), issue(78, "Auth bypass", created_at="2026-09-04T03:02:00Z")

    def handler(request):
        return httpx.Response(200, json=[two, one])

    r = run(contract({"repo": "acme/api", "kind": "issue", "number": None, "title": "Auth bypass"}), handler, settings)
    assert r.verdict == Verdict.UNKNOWN
    assert r.results[0].status == ConditionStatus.UNKNOWN


def test_preexisting_match_is_not_current_evidence(settings):
    old = issue(10, "Auth bypass", created_at="2026-09-04T02:59:59Z")

    def handler(request):
        return httpx.Response(200, json=[old])

    r = run(contract({"repo": "acme/api", "kind": "issue", "number": None, "title": "Auth bypass"}), handler, settings)
    assert r.verdict == Verdict.FAILED


def test_privacy_preserving_404_is_unknown(settings):
    def handler(request):
        return httpx.Response(404, json={"message": "Not Found"})

    r = run(contract({"repo": "acme/private", "kind": "issue", "number": 4}), handler, settings)
    assert r.verdict == Verdict.UNKNOWN


def test_transient_github_failure_retries(settings):
    candidate = issue(77, "Auth bypass")
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, json={})
        if request.url.path.endswith("/issues"):
            return httpx.Response(200, json=[candidate])
        return httpx.Response(200, json=candidate)

    r = run(contract({"repo": "acme/api", "kind": "issue", "number": None, "title": "Auth bypass"}), handler, settings)
    assert r.verdict == Verdict.VERIFIED
    assert calls["n"] >= 3


def test_caller_backdated_created_after_cannot_override_contract_boundary(settings):
    old = issue(10, "Auth bypass", created_at="2026-09-04T02:59:59Z")

    def handler(request):
        if request.url.path.endswith("/issues"):
            assert request.url.params["since"].startswith("2026-09-04T03:00:00")
            return httpx.Response(200, json=[old])
        raise AssertionError(request.url)

    c = contract(
        {
            "repo": "acme/api",
            "kind": "issue",
            "number": None,
            "title": "Auth bypass",
            "created_after": "1999-01-01T00:00:00Z",
        }
    )
    assert run(c, handler, settings).verdict == Verdict.FAILED
