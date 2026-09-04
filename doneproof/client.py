from __future__ import annotations

from typing import Any

import httpx
from pydantic import ValidationError

from . import __version__
from .domain import VerificationReceipt
from .signing import ReceiptSigner


class DoneProofClient:
    """Minimal synchronous client for pilot integrations."""

    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 30.0, transport=None):
        headers = {"Accept": "application/json", "User-Agent": f"doneproof-python/{__version__}"}
        if api_key:
            headers["X-DoneProof-Key"] = api_key
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def capabilities(self) -> dict[str, Any]:
        return self._json(self._client.get("/v1/capabilities"))

    def signing_key(self) -> dict[str, Any]:
        """Return the deployment public signing key to pin out-of-band."""
        return self._json(self._client.get("/v1/signing-key"))

    def register_run(self, contract: dict[str, Any]) -> dict[str, Any]:
        return self._json(self._client.post("/v1/runs", json={"contract": contract}))

    def verify_run(self, contract_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._json(self._client.post(f"/v1/runs/{contract_id}/verify", headers=headers))

    def verify(self, contract: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._json(self._client.post("/v1/verify", json={"contract": contract}, headers=headers))

    def receipt(self, receipt_id: str) -> dict[str, Any]:
        return self._json(self._client.get(f"/v1/receipts/{receipt_id}"))

    def evidence_bundle(self, receipt_id: str) -> dict[str, Any]:
        return self._json(self._client.get(f"/v1/receipts/{receipt_id}/bundle"))

    @staticmethod
    def verify_receipt(receipt: dict[str, Any], trusted_public_key_b64: str) -> bool:
        """Verify integrity and require the receipt signer to match a pinned key."""
        try:
            parsed = VerificationReceipt.model_validate(receipt)
        except (ValidationError, TypeError, ValueError):
            return False
        return ReceiptSigner.verify_trusted(parsed, trusted_public_key_b64)

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        return response.json()
