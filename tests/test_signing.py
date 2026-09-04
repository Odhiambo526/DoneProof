import asyncio

from doneproof.domain import CompletionContract
from doneproof.engine import VerificationEngine
from doneproof.signing import ReceiptSigner
from tests.fakes import MockAdapter


def test_receipt_signature_verifies_and_detects_tampering(settings):
    c = CompletionContract.model_validate(
        {
            "task": "Send invoice",
            "postconditions": [
                {
                    "id": "p1",
                    "description": "sent",
                    "provider": "unresolved",
                    "selector": {"state": {"sent": True}},
                    "predicate": {"op": "eq", "path": "sent", "expected": True},
                    "required": True,
                }
            ],
        }
    )
    signer = ReceiptSigner(settings)
    receipt = asyncio.run(VerificationEngine({"unresolved": MockAdapter()}, signer).verify(c))
    assert ReceiptSigner.verify(receipt)
    receipt.task = "Tampered task"
    assert not ReceiptSigner.verify(receipt)


def test_trusted_receipt_verification_rejects_a_different_self_signed_issuer(settings):
    import base64
    from dataclasses import replace

    c = CompletionContract.model_validate(
        {
            "task": "Send invoice",
            "postconditions": [
                {
                    "id": "p1",
                    "description": "sent",
                    "provider": "unresolved",
                    "selector": {"state": {"sent": True}},
                    "predicate": {"op": "eq", "path": "sent", "expected": True},
                    "required": True,
                }
            ],
        }
    )
    trusted_signer = ReceiptSigner(settings)
    trusted_receipt = asyncio.run(VerificationEngine({"unresolved": MockAdapter()}, trusted_signer).verify(c))
    assert ReceiptSigner.verify_trusted(trusted_receipt, trusted_signer.public_key_b64)

    attacker_settings = replace(settings, signing_seed_b64=base64.b64encode(b"A" * 32).decode())
    attacker_signer = ReceiptSigner(attacker_settings)
    forged = asyncio.run(VerificationEngine({"unresolved": MockAdapter()}, attacker_signer).verify(c))

    # The forged record is internally self-consistent, but it is not from the
    # pinned DoneProof signer and therefore must not be trusted as authentic.
    assert ReceiptSigner.verify(forged)
    assert not ReceiptSigner.verify_trusted(forged, trusted_signer.public_key_b64)


def test_signer_rejects_malformed_base64_seed(settings):
    from dataclasses import replace

    import pytest

    bad = replace(settings, signing_seed_b64="!!!!not-base64!!!!")
    with pytest.raises(Exception):
        ReceiptSigner(bad)
