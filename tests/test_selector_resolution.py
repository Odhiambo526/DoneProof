from __future__ import annotations

import asyncio

from doneproof.adapters.base import ProviderAdapter, ProviderObservation
from doneproof.intent import fast_candidate
from doneproof.selector_resolution import SelectorResolver
from evaluations.run_compiler import FixtureConnections


def test_provider_preflight_concurrency_is_bounded(settings):
    class Adapter(ProviderAdapter):
        active = 0
        peak = 0
        async def observe(self, selector, context):
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(.01)
            self.active -= 1
            return ProviderObservation({"number": selector["number"]})
    adapter = Adapter()
    resolver = SelectorResolver({"github": adapter}, FixtureConnections("available"), settings)
    candidate = fast_candidate("; ".join(f"Close issue #{n} in acme/api" for n in range(1, 21)))
    checks, issues = asyncio.run(resolver.resolve(candidate, "a", {"github": "available"}))
    assert not issues
    assert len(checks) == 20
    assert adapter.peak == 4


def test_changed_discovery_constraints_are_not_silently_pinned(settings):
    class Adapter(ProviderAdapter):
        async def observe(self, selector, context):
            return ProviderObservation({"number": 123, "title": "Renamed during search"})
    resolver = SelectorResolver({"github": Adapter()}, FixtureConnections("available"), settings)
    candidate = fast_candidate('Close issue in acme/api titled "Customer request"')
    checks, issues = asyncio.run(resolver.resolve(candidate, "a", {"github": "available"}))
    assert issues and checks[0].status == "unavailable"
    assert "number" not in candidate.postconditions[0].selector
