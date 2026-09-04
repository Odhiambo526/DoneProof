from __future__ import annotations

from .base import ObservationContext, ProviderAdapter, ProviderObservation


class MockAdapter(ProviderAdapter):
    """Synthetic adapter available only for tests and explicitly enabled demos."""

    async def observe(self, selector: dict, context: ObservationContext):
        if "state" not in selector:
            raise ValueError("mock selector requires a 'state' object")
        return ProviderObservation(
            state=selector["state"],
            source_url="mock://local-state",
            note="Synthetic evidence; not suitable for production assurance.",
        )
