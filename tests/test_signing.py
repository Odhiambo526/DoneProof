from doneproof.domain import CompletionContract
from doneproof.engine import VerificationEngine
from tests.fakes import MockAdapter
from doneproof.signing import ReceiptSigner
import asyncio


def test_receipt_signature_verifies_and_detects_tampering(settings):
    c = CompletionContract.model_validate({"task":"Send invoice","postconditions":[{"id":"p1","description":"sent","provider":"unresolved","selector":{"state":{"sent":True}},"predicate":{"op":"eq","path":"sent","expected":True},"required":True}]})
    signer = ReceiptSigner(settings)
    receipt = asyncio.run(VerificationEngine({"unresolved": MockAdapter()}, signer).verify(c))
    assert ReceiptSigner.verify(receipt)
    receipt.task = "Tampered task"
    assert not ReceiptSigner.verify(receipt)
