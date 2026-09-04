import asyncio

from doneproof.adapters.mock import MockAdapter
from doneproof.domain import CompletionContract, Verdict
from doneproof.engine import VerificationEngine
from doneproof.signing import ReceiptSigner


def run(contract, settings):
    return asyncio.run(VerificationEngine({"mock": MockAdapter()}, ReceiptSigner(settings), timeout_seconds=1).verify(contract))


def contract(conditions):
    return CompletionContract.model_validate({"task": "Verify requested outcome", "postconditions": conditions})


def test_verified_when_all_required_pass(settings):
    c = contract([{"id":"p1","description":"state changed","provider":"mock","selector":{"state":{"sent":True}},"predicate":{"op":"eq","path":"sent","expected":True},"required":True}])
    r = run(c, settings)
    assert r.verdict == Verdict.VERIFIED
    assert r.summary.passed == 1
    assert len(r.receipt_hash) == 64
    assert r.signature_alg == "Ed25519"


def test_partial_when_required_pass_and_fail(settings):
    c = contract([
        {"id":"p1","description":"created","provider":"mock","selector":{"state":{"created":True}},"predicate":{"op":"eq","path":"created","expected":True},"required":True},
        {"id":"p2","description":"assigned","provider":"mock","selector":{"state":{"assignees":[]}},"predicate":{"op":"contains","path":"assignees","expected":"alice"},"required":True},
    ])
    assert run(c, settings).verdict == Verdict.PARTIAL


def test_unknown_required_dominates_incomplete_verdict(settings):
    c = contract([
        {"id":"p1","description":"created","provider":"mock","selector":{"state":{"created":True}},"predicate":{"op":"eq","path":"created","expected":True},"required":True},
        {"id":"p2","description":"external proof","provider":"unresolved","selector":{"reason":"missing id"},"predicate":{"op":"exists","path":"","expected":None},"required":True},
    ])
    assert run(c, settings).verdict == Verdict.UNKNOWN


def test_optional_failure_does_not_downgrade_required_success(settings):
    c = contract([
        {"id":"p1","description":"sent","provider":"mock","selector":{"state":{"sent":True}},"predicate":{"op":"eq","path":"sent","expected":True},"required":True},
        {"id":"p2","description":"optional label","provider":"mock","selector":{"state":{"labels":[]}},"predicate":{"op":"contains","path":"labels","expected":"nice-to-have"},"required":False},
    ])
    assert run(c, settings).verdict == Verdict.VERIFIED


def test_sensitive_selector_fields_are_redacted(settings):
    c = contract([{"id":"p1","description":"safe evidence","provider":"mock","selector":{"state":{"ok":True},"api_key":"super-secret"},"predicate":{"op":"eq","path":"ok","expected":True},"required":True}])
    r = run(c, settings)
    assert r.results[0].evidence.selector["api_key"] == "[REDACTED]"
    assert "super-secret" not in r.model_dump_json()
