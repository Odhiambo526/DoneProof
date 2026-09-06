"""One immutable provider catalog per application and worker."""
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import entry_points
from types import MappingProxyType

from .provider_sdk import AdapterRuntime, ProviderDefinition
from .retries import RetryPolicy

UNRESOLVED_POLICY = RetryPolicy(1, 1.0, 1.0)


@dataclass(frozen=True, init=False, eq=False)
class ProviderRegistry:
    def __init__(self, definitions):
        providers = {}
        for definition in definitions:
            if not isinstance(definition, ProviderDefinition):
                raise TypeError("Provider registration requires an SDK definition")
            name = definition.manifest.provider_id
            if name in providers:
                raise ValueError("Duplicate provider registration: " + name)
            providers[name] = definition
        object.__setattr__(self, "_providers", MappingProxyType(providers))

    def __iter__(self):
        return iter(self._providers.values())

    def get(self, name):
        return self._providers.get(name)

    def require(self, name):
        definition = self.get(name)
        if definition is None:
            raise ValueError("Provider is not installed")
        return definition

    def accepts(self, contract):
        return all(pc.provider == "unresolved" or (self.get(pc.provider) and self.require(pc.provider).admit_condition(pc))
                   for pc in contract.postconditions)

    def concurrency(self):
        return {**{d.manifest.provider_id: d.manifest.rate_limit.concurrency for d in self}, "unresolved": 16}

    def policy(self, name):
        definition = self.get(name)
        return definition.manifest.rate_limit.retry_policy() if definition else UNRESOLVED_POLICY

    def adapters(self, settings, store, connections):
        from .connections import ManagedAdapter
        return {d.manifest.provider_id: ManagedAdapter(connections, d.manifest.provider_id)
                if d.connection_factory else d.build(AdapterRuntime(settings, store)) for d in self}

    def capability(self, tenant, provider, connections, settings):
        definition = self.get(provider)
        if not definition:
            return "configuration_required"
        if definition.connection_factory:
            return connections.capability(tenant, provider)
        return definition.capability(tenant, settings)

    def documents(self):
        return [{**d.manifest.model_dump(mode="json"), "fingerprint": d.fingerprint} for d in self]

    def describe_contracts(self, schema):
        """Keep OpenAPI admission vocabulary consistent with the installed catalog."""
        if isinstance(schema, dict):
            props = schema.get("properties", {})
            if {"provider", "selector", "predicate"} <= props.keys():
                props["provider"]["enum"] = [d.manifest.provider_id for d in self] + ["unresolved"]
            for value in schema.values():
                self.describe_contracts(value)
        elif isinstance(schema, list):
            for value in schema:
                self.describe_contracts(value)
        return schema


@lru_cache(maxsize=2)
def default_registry(*, plugins=False):
    from .adapters.builtin_provider import builtin_definitions
    definitions = list(builtin_definitions())
    if plugins:
        # Only the installed environment supplies entry points. No runtime uploads,
        # user-selected imports, or remote plugin discovery are supported.
        for entry in sorted(entry_points(group="doneproof.providers"), key=lambda e: e.name):
            definition = entry.load()()
            if not isinstance(definition, ProviderDefinition) or entry.name != definition.manifest.provider_id:
                raise ValueError("Provider entry point does not match its declaration")
            definitions.append(definition)
    return ProviderRegistry(definitions)
