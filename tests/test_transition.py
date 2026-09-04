import asyncio

from doneproof.adapters.base import ObservationContext, ProviderAdapter, ProviderObservation
from doneproof.domain import CompletionContract, Verdict
from doneproof.engine import VerificationEngine
from doneproof.signing import ReceiptSigner


class SequenceAdapter(ProviderAdapter):
    def __init__(self, values):
        self.values = list(values)

    async def observe(self, selector, context: ObservationContext):
        value = self.values.pop(0) if len(self.values) > 1 else self.values[0]
        return ProviderObservation(state={"assigned": value}, source_url="test://record")


def transition_contract():
    return CompletionContract.model_validate(
        {
            "task": "Assign Alice to existing ticket",
            "postconditions": [
                {
                    "id": "p1",
                    "description": "Alice becomes assigned",
                    "provider": "unresolved",
                    "selector": {"record": "42"},
                    "predicate": {"op": "eq", "path": "assigned", "expected": True},
                    "required": True,
                    "require_change": True,
                }
            ],
        }
    )


def test_registered_transition_from_false_to_true_is_verified(settings):
    adapter = SequenceAdapter([False, True])
    engine = VerificationEngine({"unresolved": adapter}, ReceiptSigner(settings))
    c = transition_contract()
    baseline = asyncio.run(engine.snapshot(c))
    r = asyncio.run(engine.verify(c, assurance_level="registered", baselines={x.id: x for x in baseline}))
    assert r.verdict == Verdict.VERIFIED
    assert "Transition verified" in r.results[0].reason


def test_preexisting_true_state_is_not_credited_to_agent(settings):
    adapter = SequenceAdapter([True, True])
    engine = VerificationEngine({"unresolved": adapter}, ReceiptSigner(settings))
    c = transition_contract()
    baseline = asyncio.run(engine.snapshot(c))
    r = asyncio.run(engine.verify(c, assurance_level="registered", baselines={x.id: x for x in baseline}))
    assert r.verdict == Verdict.FAILED
    assert "already satisfied" in r.results[0].reason


def test_transition_required_without_registered_baseline_is_unknown(settings):
    adapter = SequenceAdapter([True])
    engine = VerificationEngine({"unresolved": adapter}, ReceiptSigner(settings))
    r = asyncio.run(engine.verify(transition_contract(), assurance_level="submitted"))
    assert r.verdict == Verdict.UNKNOWN


def test_transition_receipt_carries_signed_baseline_evidence(settings):
    adapter = SequenceAdapter([False, True])
    engine = VerificationEngine({"unresolved": adapter}, ReceiptSigner(settings))
    c = transition_contract()
    baseline = asyncio.run(engine.snapshot(c))
    r = asyncio.run(engine.verify(c, assurance_level="registered", baselines={x.id: x for x in baseline}))
    result = r.results[0]
    assert result.transition_required is True
    assert result.baseline_status.value == "FAIL"
    assert result.baseline_observed is False
