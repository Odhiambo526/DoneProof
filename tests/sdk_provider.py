"""A fourth provider, entirely outside DoneProof core, used for SDK contract tests."""
import asyncio
import base64
import hashlib
import re
import time
from urllib.parse import urlencode

from doneproof.adapters.base import ProviderAdapter, ProviderObservation
from doneproof.provider_compilation import SchemaCompiler
from doneproof.provider_registry import ProviderRegistry, default_registry
from doneproof.provider_sdk import ProviderDefinition
from doneproof.retries import TransientObservationError

ACCESS = "sdk-access-secret-sentinel"
REFRESH = "sdk-refresh-secret-sentinel"
PRIVATE = "sdk-private-evidence-sentinel"


class InventoryCompiler(SchemaCompiler):
    def parse_clause(self, clause, context):
        match = re.fullmatch(r"(Verify|Change) inventory item ([a-z0-9-]+) (?:is|to) (ready|pending)", clause)
        if not match:
            return None
        return ("inventory", {"item_id": match[2]}, [("eq", "status", match[3])],
                "transition" if match[1] == "Change" else "state")


class InventoryBackend:
    """Deterministic OAuth provider simulator; it never performs external actions."""
    def __init__(self, settings, transport=None):
        pass

    def configured(self, provider):
        return True

    def authorize_url(self, provider, state, verifier, redirect_uri):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return "https://inventory.example/authorize?" + urlencode({"state": state,
            "redirect_uri": redirect_uri, "code_challenge": challenge, "code_challenge_method": "S256"})

    async def exchange(self, provider, code, verifier, redirect_uri):
        return {"access_token": ACCESS, "refresh_token": REFRESH, "kind": "oauth",
                "expires_at": int(time.time()) + 3600, "scopes": ["inventory.read"]}

    async def refresh(self, provider, credentials):
        return {**credentials, "expires_at": int(time.time()) + 3600, "access_token": ACCESS + "-rotated"}

    async def identity(self, provider, credentials):
        return "inventory-account", "Inventory account"

    async def revoke(self, provider, credentials):
        pass


class InventoryAdapter(ProviderAdapter):
    def __init__(self, runtime, state):
        self.runtime, self.state = runtime, state

    async def observe(self, selector, context):
        self.state["calls"].append((context.tenant_id, selector.copy(), context.contract_id))
        self.state["active"] += 1
        self.state["peak"] = max(self.state["peak"], self.state["active"])
        try:
            await asyncio.sleep(0.005)
            if self.state.get("transient"):
                raise TransientObservationError(retry_after=45)
            if self.state.get("unknown"):
                return ProviderObservation(None, indeterminate=True)
            return ProviderObservation({"item_id": selector["item_id"],
                "status": self.state.get(context.tenant_id, "pending"),
                "internal_note": PRIVATE, "access_token": ACCESS,
                **self.state.get("extra", {})})
        finally:
            self.state["active"] -= 1


def definition(state=None, *, version="1.0.0", connection_factory=InventoryBackend, **changes):
    state = state if state is not None else {"calls": [], "active": 0, "peak": 0, "tenant-a": "ready"}
    manifest = {
        "provider_id": "inventory", "version": version, "display_name": "Inventory",
        "description": "Read-only inventory status from an authoritative account.",
        "resource_types": ("item",),
        "evidence_schema": {"type": "object", "properties": {"item_id": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "ready"]}, "internal_note": {"type": "string"}},
            "required": ["item_id", "status"], "additionalProperties": False},
        "selector_schema": {"type": "object", "properties": {"item_id": {"type": "string", "pattern": "^[a-z0-9-]+$"}},
                            "required": ["item_id"], "additionalProperties": False},
        "supported_predicates": ("eq", "neq"),
        "discovery": {"supported": False, "identity_field": "item_id", "identity_schema": {"type": "string", "pattern": "^[a-z0-9-]+$"}},
        "baseline_support": True, "transition_support": True,
        "authentication": {"mode": "managed_oauth", "requirements": ("inventory.read",),
                           "authorization_origin": "https://inventory.example", "refresh_required": True},
        "rate_limit": {"concurrency": 2, "preflight_concurrency": 1, "attempts": 2, "base_seconds": 1.0, "cap_seconds": 10.0},
        "evidence_sensitivity": "restricted", "sensitive_paths": ("internal_note",),
        "context_fields": ("item_id", "status"),
        "compiler_instructions": "Verify an exact inventory item status. Never infer a resource identifier.",
        **changes,
    }
    return ProviderDefinition(manifest, lambda runtime: InventoryAdapter(runtime, state), InventoryCompiler(manifest),
                              connection_factory=connection_factory)


def registry(state=None, **kwargs):
    return ProviderRegistry([*default_registry(), definition(state, **kwargs)])


def payload(size=1, *, path="status", expected="ready", provider="inventory"):
    return {"contract": {"task": "Verify inventory items are ready", "postconditions": [
        {"id": f"p{i}", "description": "Inventory status", "provider": provider,
         "selector": {"item_id": f"item-{i}"}, "predicate": {"op": "eq", "path": path, "expected": expected}}
        for i in range(size)]}}
