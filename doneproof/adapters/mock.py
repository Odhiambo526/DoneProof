from __future__ import annotations

from .base import ProviderAdapter, ProviderObservation


class MockAdapter(ProviderAdapter):
    """Deterministic adapter used for tests, demos, and SDK integration work."""

    async def observe(self, selector: dict):
        if "state" not in selector:
            raise ValueError("mock selector requires a 'state' object")
        return ProviderObservation(
            state=selector["state"],
            source_url="mock://local-state",
            note="Synthetic state supplied by the caller; not independent external evidence.",
        )
