import asyncio

from doneproof.adapters.mock import MockAdapter
from doneproof.domain import CompletionContract, Verdict
from doneproof.engine import VerificationEngine


def run(contract):
    return asyncio.run(VerificationEngine({"mock": MockAdapter()}, receipt_key="test").verify(contract))


def test_verified_when_all_required_pass():
    c = CompletionContract.model_validate({
        "task": "Send a thing",
        "postconditions": [{
            "id": "p1", "description": "state changed", "provider": "mock",
            "selector": {"state": {"sent": True}},
            "predicate": {"op": "eq", "path": "sent", "expected": True}, "required": True
        }]
    })
    r = run(c)
    assert r.verdict == Verdict.VERIFIED
    assert len(r.receipt_hash) == 64
    assert len(r.signature) == 64


def test_partial_when_one_required_passes_and_one_fails():
    c = CompletionContract.model_validate({
        "task": "Create and assign issue",
        "postconditions": [
            {"id":"p1","description":"created","provider":"mock","selector":{"state":{"created":True}},"predicate":{"op":"eq","path":"created","expected":True},"required":True},
            {"id":"p2","description":"assigned","provider":"mock","selector":{"state":{"assignees":[]}},"predicate":{"op":"contains","path":"assignees","expected":"alice"},"required":True}
        ]
    })
    assert run(c).verdict == Verdict.PARTIAL


def test_failed_when_only_required_condition_fails():
    c = CompletionContract.model_validate({
        "task": "Submit form",
        "postconditions": [{
            "id":"p1","description":"submitted","provider":"mock","selector":{"state":{"status":"draft"}},
            "predicate":{"op":"eq","path":"status","expected":"submitted"},"required":True
        }]
    })
    assert run(c).verdict == Verdict.FAILED
