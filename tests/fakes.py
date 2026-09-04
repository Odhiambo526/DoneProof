from __future__ import annotations

from typing import Any

from doneproof.adapters.base import ObservationContext, ProviderAdapter, ProviderObservation


class MockAdapter(ProviderAdapter):
    """Test-only adapter for deterministic verification fixtures."""

    async def observe(self, selector: dict[str, Any], context: ObservationContext) -> ProviderObservation:
        state = selector.get("state")
        if not isinstance(state, dict):
            raise ValueError("test selector requires a 'state' object")
        return ProviderObservation(state=state, source_url="test://local-state")
