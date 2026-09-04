from __future__ import annotations

from typing import Any

import httpx


class DoneProofClient:
    """Minimal synchronous client for pilot integrations."""

    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 30.0, transport=None):
        headers = {"Accept": "application/json", "User-Agent": "doneproof-python/0.9"}
        if api_key:
            headers["X-DoneProof-Key"] = api_key
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def capabilities(self) -> dict[str, Any]:
        return self._json(self._client.get("/v1/capabilities"))

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
    def _json(response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        return response.json()
