from doneproof.domain import CompletionContract
from doneproof.engine import VerificationEngine
from doneproof.adapters.mock import MockAdapter
from doneproof.signing import ReceiptSigner
import asyncio


def test_receipt_signature_verifies_and_detects_tampering(settings):
    c = CompletionContract.model_validate({"task":"Send invoice","postconditions":[{"id":"p1","description":"sent","provider":"mock","selector":{"state":{"sent":True}},"predicate":{"op":"eq","path":"sent","expected":True},"required":True}]})
    signer = ReceiptSigner(settings)
    receipt = asyncio.run(VerificationEngine({"mock": MockAdapter()}, signer).verify(c))
    assert ReceiptSigner.verify(receipt)
    receipt.task = "Tampered task"
    assert not ReceiptSigner.verify(receipt)
