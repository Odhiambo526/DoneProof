"""Internal provider SDK v1. Registration loads trusted installed code, never URLs.

Adapters only observe. Predicates, tenant authorization, baseline evaluation,
redaction, job ownership and signing remain owned by DoneProof.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Literal, Protocol

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .adapters.base import ProviderAdapter
from .retries import RetryPolicy

PROVIDER_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
PREDICATES = frozenset({"eq", "neq", "exists", "not_exists", "contains", "contains_all", "gte", "lte"})


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RateLimitPolicy(Declaration):
    concurrency: int = Field(ge=1, le=64)
    preflight_concurrency: int = Field(ge=1, le=64)
    attempts: int = Field(ge=1, le=8)
    base_seconds: float = Field(gt=0, le=300, allow_inf_nan=False)
    cap_seconds: float = Field(gt=0, le=3600, allow_inf_nan=False)

    @model_validator(mode="after")
    def bounded(self):
        if self.cap_seconds < self.base_seconds or self.preflight_concurrency > self.concurrency:
            raise ValueError("Invalid provider rate policy")
        return self

    def retry_policy(self):
        return RetryPolicy(self.attempts, self.base_seconds, self.cap_seconds)


class Authentication(Declaration):
    mode: Literal["managed_oauth", "signed_events", "none"]
    requirements: tuple[str, ...]
    public_read: bool = False
    refresh_required: bool = False
    authorization_origin: str | None = None
    onboarding_order: int = Field(default=100, ge=0, le=1000)

    @model_validator(mode="after")
    def fixed_origin(self):
        from urllib.parse import urlsplit
        if self.mode == "managed_oauth":
            value = self.authorization_origin or ""
            parts = urlsplit(value)
            if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                    or parts.path or parts.query or parts.fragment or parts.port not in (None, 443)):
                raise ValueError("OAuth requires a fixed HTTPS authorization origin")
        elif self.authorization_origin or self.refresh_required or self.public_read:
            raise ValueError("Connection options require managed OAuth")
        return self


class Discovery(Declaration):
    supported: bool
    identity_field: str
    identity_schema: dict
    scope_fields: tuple[str, ...] = ()
    boundary_field: str | None = None
    event_driven: bool = False

    def is_discovery(self, selector):
        return self.supported and (self.event_driven or selector.get(self.identity_field) is None)


class ProviderManifest(Declaration):
    sdk_version: Literal[1] = 1
    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    display_name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    resource_types: tuple[str, ...] = Field(min_length=1)
    evidence_schema: dict
    selector_schema: dict
    supported_predicates: tuple[str, ...] = Field(min_length=1)
    discovery: Discovery
    baseline_support: bool
    transition_support: bool
    authentication: Authentication
    rate_limit: RateLimitPolicy
    evidence_sensitivity: Literal["public", "internal", "confidential", "restricted"]
    sensitive_paths: tuple[str, ...] = ()
    context_fields: tuple[str, ...] = ()
    compiler_instructions: str = Field(default="", max_length=8000)

    @model_validator(mode="after")
    def valid_contract(self):
        if self.provider_id == "unresolved":
            raise ValueError("unresolved is a reserved non-provider sentinel")
        if (not set(self.supported_predicates) <= PREDICATES
                or len(set(self.supported_predicates)) != len(self.supported_predicates)
                or self.transition_support and not self.baseline_support):
            raise ValueError("Invalid predicate or transition declaration")
        for schema in (self.evidence_schema, self.selector_schema, self.discovery.identity_schema):
            validate_schema(schema)
        if self.selector_schema.get("type") != "object" or self.selector_schema.get("additionalProperties") is not False:
            raise ValueError("Selector schemas must explicitly close their property set")
        if not self.discovery.event_driven and self.discovery.identity_field not in self.selector_schema.get("properties", {}):
            raise ValueError("The authoritative identity must be a declared selector")
        if self.discovery.event_driven and not self.discovery.supported:
            raise ValueError("Event discovery must be declared supported")
        properties = self.selector_schema.get("properties", {})
        if set(self.discovery.scope_fields) - properties.keys() or (self.discovery.boundary_field and self.discovery.boundary_field not in properties):
            raise ValueError("Discovery scope and boundary fields must be declared selectors")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", path) for path in self.sensitive_paths):
            raise ValueError("Sensitivity paths must name fields; redact containing arrays as a whole")
        for path in self.sensitive_paths:
            schema = self.evidence_schema
            for part in path.split("."):
                schema = schema.get("properties", {}).get(part)
                if schema is None:
                    raise ValueError("Sensitivity paths must resolve to declared evidence fields")
        return self


def validate_schema(schema):
    # References cannot trigger network or filesystem resolution, even in documentation/tests.
    def walk(value):
        if isinstance(value, dict):
            if any(k in value for k in ("$ref", "$dynamicRef", "$id")):
                raise ValueError("Provider schemas must be self-contained without references")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    if len(json.dumps(schema, allow_nan=False)) > 65536:
        raise ValueError("Provider schema is too large")
    walk(schema)
    Draft202012Validator.check_schema(schema)


class CompilerHooks(Protocol):
    def parse_clause(self, clause, context): ...
    def analyze_condition(self, pc, task, context): ...
    def analyze_intent(self, intent, targets): ...
    def validate_legacy_selector(self, pc): ...


class ConnectionBackend(Protocol):
    """All methods use fixed provider endpoints and return sanitized error codes.

    Credentials stay inside the service. identity must verify read-only scopes and
    return (stable_account_id, safe_account_label). No business action method exists.
    """
    def configured(self, provider): ...
    def authorize_url(self, provider, state, verifier, redirect_uri): ...
    async def exchange(self, provider, code, verifier, redirect_uri): ...
    async def refresh(self, provider, credentials): ...
    async def identity(self, provider, credentials): ...
    async def revoke(self, provider, credentials): ...


@dataclass(frozen=True)
class AdapterRuntime:
    settings: object
    store: object
    tenant_id: str | None = None
    credentials: dict | None = field(default=None, repr=False)
    transport: object = None
    response_hooks: tuple = ()


@dataclass(frozen=True, init=False)
class ProviderDefinition:
    """Immutable metadata snapshot plus trusted, provider-owned implementation hooks."""
    _manifest_json: str
    adapter_factory: Callable
    compiler: CompilerHooks
    connection_factory: Callable | None
    capability: Callable | None
    event_selector_allowed: Callable | None
    legacy_credentials: Callable
    validate_configuration: Callable
    installation_url: Callable
    admit_condition: Callable

    def __init__(self, manifest, adapter_factory, compiler=None, *, connection_factory=None, capability=None,
                 event_selector_allowed=None, legacy_credentials=lambda settings: (),
                 validate_configuration=lambda settings: None, installation_url=lambda settings: None,
                 admit_condition=lambda pc: True):
        manifest = ProviderManifest.model_validate(manifest)
        if compiler is None:
            from .provider_compilation import SchemaCompiler
            compiler = SchemaCompiler(manifest)
        if bool(connection_factory) != (manifest.authentication.mode == "managed_oauth"):
            raise ValueError("Managed OAuth requires a connection backend")
        if manifest.authentication.mode != "managed_oauth" and not callable(capability):
            raise ValueError("Unmanaged providers must declare tenant-bound availability")
        if manifest.discovery.event_driven and not callable(event_selector_allowed):
            raise ValueError("Event discovery requires a tenant-bound source authorization hook")
        if not callable(adapter_factory) or not callable(admit_condition) or not all(callable(getattr(compiler, name, None)) for name in
                ("parse_clause", "analyze_condition", "analyze_intent", "validate_legacy_selector")):
            raise ValueError("Provider implementation does not implement SDK v1")
        values = locals()
        object.__setattr__(self, "_manifest_json", json.dumps(manifest.model_dump(mode="json"), sort_keys=True,
                                                            separators=(",", ":"), allow_nan=False))
        for name in self.__dataclass_fields__:
            if name != "_manifest_json":
                object.__setattr__(self, name, values[name])

    @property
    def manifest(self):
        # Nested schema mutations by a caller cannot change a running deployment.
        return _validated_manifest(self._manifest_json).model_copy(deep=True)

    @property
    def fingerprint(self):
        return hashlib.sha256(self._manifest_json.encode()).hexdigest()

    def build(self, runtime):
        adapter = self.adapter_factory(runtime)
        if not isinstance(adapter, ProviderAdapter):
            raise TypeError("Provider factories must return ProviderAdapter instances")
        return adapter


@lru_cache(maxsize=256)
def _validated_manifest(value):
    return ProviderManifest.model_validate_json(value)
