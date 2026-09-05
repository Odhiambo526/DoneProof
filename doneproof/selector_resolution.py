"""Tenant-bound, read-only planning checks using the same authoritative adapters."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

from .adapters.base import ObservationContext
from .compilation_models import SelectorCheck, issue
from .contract_analysis import ID


class SelectorResolver:
    def __init__(self, adapters, connections, settings):
        self.adapters, self.connections, self.settings = adapters, connections, settings
        self.limits = {"github": asyncio.Semaphore(4), "gmail": asyncio.Semaphore(2), "webhook": asyncio.Semaphore(4)}

    async def capabilities(self, tenant):
        result = {}
        for provider in ("github", "gmail"):
            state = self.connections.capability(tenant, provider)
            if state == "configuration_required":
                # An expired access token with a usable refresh token is recoverable.
                await self.connections.usable(tenant, provider)
                state = self.connections.capability(tenant, provider)
            result[provider] = state
        result["webhook"] = "available" if any(s.tenant_id == tenant for s in self.settings.webhook_sources.values()) else "configuration_required"
        return result

    async def resolve(self, candidate, tenant, capabilities):
        modes = {ident: intent.mode for intent in candidate.intents for ident in intent.condition_ids}
        groups = {}
        for pc in candidate.postconditions:
            key = (pc.provider, json.dumps(pc.selector, sort_keys=True), modes[pc.id])
            groups.setdefault(key, []).append(pc)
        # All selectors in one compilation share a server-generated preflight boundary.
        now = datetime.now(timezone.utc).isoformat()
        results = await asyncio.gather(*(self._group(pcs, tenant, modes[pcs[0].id], capabilities, now)
                                         for pcs in groups.values()))
        return [r[0] for r in results], [r[1] for r in results if r[1] is not None]

    async def _group(self, pcs, tenant, mode, capabilities, now):
        pc = pcs[0]
        ids = [p.id for p in pcs]
        def result(status, code, category=None):
            return (SelectorCheck(condition_ids=ids, status=status, code=code),
                    issue(code, category or "unverifiable_outcome", ids=ids) if category else None)
        if capabilities.get(pc.provider) != "available":
            return result("unavailable", "provider_unavailable", "unverifiable_outcome")
        if pc.provider == "webhook":
            source = self.settings.webhook_sources.get(pc.selector.get("source"))
            if source is None or source.tenant_id != tenant:
                return result("unavailable", "provider_unavailable", "unverifiable_outcome")
            if mode != "event":
                return result("unavailable", "unsupported_outcome", "unverifiable_outcome")
            return result("deferred", "future_discovery")
        adapter = self.adapters[pc.provider]
        selector = dict(pc.selector)
        discovery = selector.get("number" if pc.provider == "github" else "message_id") is None
        if discovery:
            # Existing-resource search starts at the epoch and must prove bounded
            # search completeness. The adapter returns UNKNOWN if the budget is exhausted.
            selector["created_after"] = now if mode == "create" else "1970-01-01T00:00:00+00:00"
        if mode == "create" and not discovery:
            return result("unavailable", "impossible_selector", "unverifiable_outcome")
        context = ObservationContext(tenant_id=tenant, contract_id="preflight_" + uuid4().hex,
                                     task_started_at=now, condition_id=pc.id)
        try:
            async with self.limits[pc.provider]:
                observation = await asyncio.wait_for(adapter.observe(selector, context), timeout=15)
            if not adapter.observation_is_current(observation.authority, tenant):
                return result("unavailable", "provider_unavailable", "unverifiable_outcome")
        except Exception:
            return result("unavailable", "provider_unavailable", "unverifiable_outcome")
        state = observation.state
        if observation.indeterminate:
            if isinstance(state, dict) and type(state.get("candidate_count")) is int and state["candidate_count"] > 1:
                return result("ambiguous", "ambiguous_resource", "ambiguous_resource")
            return result("unavailable", "provider_unavailable", "unverifiable_outcome")
        if mode == "create":
            # Compilation never pins a future creation to a resource already observed.
            return result("deferred", "future_discovery")
        if not isinstance(state, dict):
            return result("missing", "resource_not_found", "missing_identifier")
        key = "number" if pc.provider == "github" else "message_id"
        value = state.get(key)
        valid_id = (type(value) is int and 0 < value < 2**53) if pc.provider == "github" else (isinstance(value, str) and bool(ID.fullmatch(value)))
        if not valid_id or (not discovery and pc.selector[key] != value):
            return result("missing", "resource_not_found", "missing_identifier")
        if discovery:
            for field, expected in pc.selector.items():
                if field in {"repo", "kind"}:
                    continue
                actual = state.get(field)
                matches = expected in actual if isinstance(actual, list) else actual == expected
                if not matches:
                    return result("unavailable", "provider_unavailable", "unverifiable_outcome")
            pinned = {"repo": pc.selector["repo"], "kind": pc.selector["kind"], "number": value} if pc.provider == "github" else {"message_id": value}
            for target in pcs:
                target.selector = dict(pinned)
        return result("resolved", "preflight_only")
