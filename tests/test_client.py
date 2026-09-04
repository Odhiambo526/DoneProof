import httpx

from doneproof.client import DoneProofClient


def test_python_client_sets_auth_and_idempotency():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path == "/v1/runs/cc_1/verify":
            return httpx.Response(200, json={"receipt_id": "vr_1"})
        return httpx.Response(200, json={})

    with DoneProofClient("https://dp.test", api_key="key-a", transport=httpx.MockTransport(handler)) as c:
        assert c.verify_run("cc_1", idempotency_key="task-1")["receipt_id"] == "vr_1"
    req = seen[0]
    assert req.headers["X-DoneProof-Key"] == "key-a"
    assert req.headers["Idempotency-Key"] == "task-1"


def test_python_client_can_verify_receipt_against_pinned_signer(settings):
    import asyncio

    from doneproof.domain import CompletionContract
    from doneproof.engine import VerificationEngine
    from doneproof.signing import ReceiptSigner
    from tests.fakes import MockAdapter

    contract = CompletionContract.model_validate(
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
    receipt = asyncio.run(VerificationEngine({"unresolved": MockAdapter()}, signer).verify(contract))
    assert DoneProofClient.verify_receipt(receipt.model_dump(mode="json"), signer.public_key_b64)


def test_python_client_receipt_verification_fails_closed_on_malformed_receipt():
    assert DoneProofClient.verify_receipt({"not": "a receipt"}, "not-base64") is False
