import asyncio
from datetime import datetime, timezone

import httpx

from doneproof.adapters.github import GitHubAdapter
from doneproof.domain import CompletionContract, ConditionStatus, Verdict
from doneproof.engine import VerificationEngine

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


def run(c, handler):
    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    return asyncio.run(VerificationEngine({"github": adapter}, receipt_key="test").verify(c))


def test_discovers_unique_issue_without_trusting_executor_number():
    candidate = issue(77, "Auth bypass", assignees=["alice"])

    def handler(request):
        if request.url.path.endswith("/issues"):
            assert request.url.params["since"].startswith("2026-09-04T03:00:00")
            return httpx.Response(200, json=[candidate])
        if request.url.path.endswith("/issues/77"):
            return httpx.Response(200, json=candidate)
        raise AssertionError(request.url)

    c = contract({"repo": "acme/api", "kind": "issue", "number": None, "title": "Auth bypass"})
    r = run(c, handler)
    assert r.verdict == Verdict.VERIFIED
    assert r.results[0].evidence.observed == "Auth bypass"
    assert "Discovered unique" in (r.results[0].evidence.note or "")


def test_duplicate_matching_titles_are_unknown_not_guessed():
    one = issue(77, "Auth bypass")
    two = issue(78, "Auth bypass", created_at="2026-09-04T03:02:00Z")

    def handler(request):
        if request.url.path.endswith("/issues"):
            return httpx.Response(200, json=[two, one])
        raise AssertionError("detail endpoint must not be called for ambiguous discovery")

    c = contract({"repo": "acme/api", "kind": "issue", "number": None, "title": "Auth bypass"})
    r = run(c, handler)
    assert r.verdict == Verdict.UNKNOWN
    assert r.results[0].status == ConditionStatus.UNKNOWN
    assert r.results[0].evidence.observed["candidate_count"] == 2


def test_wrong_issue_created_after_start_does_not_satisfy_contract():
    wrong = issue(79, "Refactor auth middleware")

    def handler(request):
        if request.url.path.endswith("/issues"):
            return httpx.Response(200, json=[wrong])
        raise AssertionError(request.url)

    c = contract({"repo": "acme/api", "kind": "issue", "number": None, "title": "Auth bypass"})
    r = run(c, handler)
    assert r.verdict == Verdict.FAILED
    assert r.results[0].status == ConditionStatus.FAIL


def test_preexisting_matching_issue_is_not_accepted_as_current_run_evidence():
    old = issue(10, "Auth bypass", created_at="2026-09-04T02:59:59Z")

    def handler(request):
        if request.url.path.endswith("/issues"):
            # A defensive client-side time bound still excludes it even if an
            # upstream/mock server ignores GitHub's `since` query parameter.
            return httpx.Response(200, json=[old])
        raise AssertionError(request.url)

    c = contract({"repo": "acme/api", "kind": "issue", "number": None, "title": "Auth bypass"})
    r = run(c, handler)
    assert r.verdict == Verdict.FAILED


def test_github_404_is_unknown_because_private_inaccessibility_is_indistinguishable():
    def handler(request):
        return httpx.Response(404, json={"message": "Not Found"})

    c = contract({"repo": "acme/private", "kind": "issue", "number": 4})
    r = run(c, handler)
    assert r.verdict == Verdict.UNKNOWN
    assert r.results[0].status == ConditionStatus.UNKNOWN
