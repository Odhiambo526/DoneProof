"""Compilation diagnostics are planning metadata, never verification evidence."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .domain import CompletionContract, Postcondition

CompilationStatus = Literal[
    "unsupported_provider", "missing_identifier", "ambiguous_resource",
    "unverifiable_outcome", "valid_contract",
]


class CompilationIssue(BaseModel):
    code: str
    category: CompilationStatus
    message: str
    condition_ids: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_text: str = Field(min_length=1, max_length=4000)
    mode: Literal["state", "transition", "create", "event", "unverifiable"]
    condition_ids: list[str] = Field(min_length=1, max_length=50)


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intents: list[Intent] = Field(min_length=1, max_length=50)
    postconditions: list[Postcondition] = Field(min_length=1, max_length=50)
    ambiguous: bool = False


class SelectorCheck(BaseModel):
    condition_ids: list[str]
    status: Literal["resolved", "deferred", "ambiguous", "unavailable", "missing"]
    # No resource bodies, tokens, candidate lists, or provider error strings.
    code: str


class CompilationUsage(BaseModel):
    model: str | None = None
    efforts: list[str] = Field(default_factory=list)
    escalation_reasons: list[str] = Field(default_factory=list)
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    # A failed HTTP request may have been billed without returning usage.
    complete: bool = True


class ContractQuality(BaseModel):
    confidence: float = Field(ge=0, le=1)
    confidence_basis: Literal["deterministic_checks"] = "deterministic_checks"
    confidence_calibrated: Literal[False] = False
    confidence_scope: Literal["contract_structure"] = "contract_structure"
    evidence: Literal[False] = False
    warnings: list[CompilationIssue] = Field(default_factory=list)
    requires_registration: bool = True


class CompilationResult(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    status: CompilationStatus
    contract: CompletionContract | None = None
    clarification_requirements: list[CompilationIssue] = Field(default_factory=list)
    contract_quality: ContractQuality
    selector_checks: list[SelectorCheck] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list)
    deterministic: bool = True
    usage: CompilationUsage = Field(default_factory=CompilationUsage)
    latency_ms: float = 0


def issue(code, category="unverifiable_outcome", *, ids=(), fields=()):
    # Fixed messages deliberately exclude untrusted model/provider content.
    messages = {
        "unsupported_provider": "This outcome has no supported authoritative provider.",
        "missing_identifier": "Supply the required resource identifiers or exact discovery constraints.",
        "ungrounded_identifier": "An identifier was not supplied by the caller or resolved by the provider.",
        "ambiguous_resource": "Multiple resources or interpretations match; supply an exact identifier or clarify the outcome.",
        "provider_unavailable": "Connect or reconnect the workspace provider, then retry compilation.",
        "resource_not_found": "The resource could not be resolved with this workspace connection.",
        "unsupported_outcome": "Specify an outcome observable through the supported provider fields.",
        "incomplete_intent": "Separate each required outcome into an explicit clause; no outcomes may be omitted.",
        "invalid_candidate": "The candidate did not satisfy the contract schema.",
        "duplicate_conditions": "Identical conditions must be consolidated.",
        "contradictory_predicates": "Conditions require incompatible states of the same resource.",
        "impossible_selector": "A selector is malformed, conflicting, or outside the provider schema.",
        "unsafe_discovery": "Discovery requires exact, bounded constraints and an unambiguous resource.",
        "meaningless_predicate": "The predicate cannot express a meaningful supported outcome.",
        "missing_transition": "A change to an existing resource requires a registered pre-execution baseline.",
        "over_broad_postcondition": "The condition does not constrain the requested business outcome sufficiently.",
        "sensitive_input": "Remove credentials and sensitive fields from the task and context.",
        "model_unavailable": "The language model is unavailable; use explicit supported clauses or retry later.",
        "compilation_deadline": "Compilation exceeded its deadline; retry or split the task.",
        "future_discovery": "New resources are resolved after execution within the registered time boundary; ambiguity remains UNKNOWN.",
        "preflight_only": "Selector checks are temporary planning checks. Verification re-observes authoritative state.",
        "model_interpretation": "Review model-interpreted intent before registering the contract.",
        "browser_lower_assurance": "Browser UI evidence has lower assurance than authoritative APIs and never substitutes for an available API.",
    }
    return CompilationIssue(code=code, category=category, message=messages[code],
                            condition_ids=list(ids), fields=list(fields))
