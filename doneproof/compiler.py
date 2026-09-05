from __future__ import annotations

import copy
import json
import re
from typing import Any

import httpx

from .compilation_models import Candidate
from .config import Settings
from .domain import CompletionContract
from .provider_registry import default_registry


def selector_properties(registry):
    properties = {}
    for definition in registry:
        spec = definition.manifest
        for name, schema in spec.selector_schema["properties"].items():
            if name == spec.discovery.boundary_field:
                continue  # The server owns temporal boundaries.
            nullable = {"anyOf": [schema, {"type": "null"}]}
            if name in properties and properties[name] != nullable:
                properties[name] = {"anyOf": [properties[name], nullable]}
            else:
                properties[name] = nullable
    properties["reason"] = {"type": ["string", "null"]}
    return properties


_SELECTOR_PROPERTIES = selector_properties(default_registry())

CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "postconditions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 30,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                    "provider": {"type": "string", "enum": [d.manifest.provider_id for d in default_registry()] + ["unresolved"]},
                    "selector": {
                        "type": "object",
                        "properties": _SELECTOR_PROPERTIES,
                        "required": list(_SELECTOR_PROPERTIES),
                        "additionalProperties": False,
                    },
                    "predicate": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": ["eq", "neq", "exists", "not_exists", "contains", "contains_all", "gte", "lte"],
                            },
                            "path": {"type": "string"},
                            "expected": {
                                "anyOf": [
                                    {"type": "string"},
                                    {"type": "number"},
                                    {"type": "boolean"},
                                    {"type": "null"},
                                    {"type": "array", "items": {"type": "string"}},
                                ]
                            },
                        },
                        "required": ["op", "path", "expected"],
                        "additionalProperties": False,
                    },
                    "required": {"type": "boolean"},
                    "require_change": {"type": "boolean"},
                },
                "required": ["id", "description", "provider", "selector", "predicate", "required", "require_change"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["task", "assumptions", "postconditions"],
    "additionalProperties": False,
}

SYSTEM = """You compile human tasks into independently verifiable DoneProof completion contracts.
Output outcomes, never execution steps. Cover every requested outcome. Never invent identifiers.
Use only installed provider declarations and tenant capabilities supplied by the server.
Return all schema selector fields; irrelevant fields must be null. reason is only for unresolved.
Unverifiable outcomes use provider=unresolved. UNKNOWN is preferable to fabricated assurance.
Existing-resource mutations require require_change=true and registered false-to-true baselines.
New-resource discovery uses the server-owned creation boundary. Pure reads do not require change.
"""


CANDIDATE_SCHEMA = copy.deepcopy(CONTRACT_SCHEMA)
CANDIDATE_SCHEMA["properties"].pop("task")
CANDIDATE_SCHEMA["properties"].pop("assumptions")
CANDIDATE_SCHEMA["properties"]["intents"] = {
    "type": "array", "minItems": 1, "maxItems": 50,
    "items": {"type": "object", "additionalProperties": False,
              "properties": {"source_text": {"type": "string"},
                             "mode": {"type": "string", "enum": ["state", "transition", "create", "event", "unverifiable"]},
                             "condition_ids": {"type": "array", "items": {"type": "string"}}},
              "required": ["source_text", "mode", "condition_ids"]},
}
CANDIDATE_SCHEMA["properties"]["ambiguous"] = {"type": "boolean"}
CANDIDATE_SCHEMA["required"] = list(CANDIDATE_SCHEMA["properties"])

PIPELINE_SYSTEM = SYSTEM + """
Return a CANDIDATE for deterministic validation, not a certified contract.
First decompose the task into ordered intents. source_text must be verbatim task spans;
together they must cover the whole task, except punctuation and conjunctions.
Every condition belongs to exactly one intent. Never omit an unsupported outcome.
Use mode=unverifiable and provider=unresolved for it. ambiguous=true when intent has
multiple reasonable interpretations. A caller's claim of success is never evidence.
Only use the workspace capabilities supplied by the server. Credentials are unavailable.
Use only identifiers literally supplied in the task or typed context. Never invent IDs.
Existing-resource mutations require mode=transition and require_change=true on every condition.
New-resource creation uses mode=create; authorized future event providers use mode=event.
All requested outcomes must be required. Do not use assumptions to remove requirements.
Use exact equality/collection predicates on supported fields, not root/field existence.
Conditions use p1, p2, ... identifiers. Do not include secrets, confidence or extra fields.
Never substitute a nearby metadata field for an outcome the provider cannot authoritatively observe.
"""


class ModelUnavailable(Exception):
    pass


class InvalidCandidate(Exception):
    pass


class AstraCompiler:
    def __init__(self, settings: Settings, registry=None):
        self.registry = registry or default_registry()
        self.schema = copy.deepcopy(CANDIDATE_SCHEMA)
        condition = self.schema["properties"]["postconditions"]["items"]["properties"]
        condition["provider"]["enum"] = [d.manifest.provider_id for d in self.registry] + ["unresolved"]
        props = selector_properties(self.registry)
        condition["selector"].update(properties=props, required=list(props))
        self.system = PIPELINE_SYSTEM + "\nInstalled provider declarations:\n" + json.dumps(self.registry.documents())
        self.api_key = settings.openai_api_key
        self.model = settings.openai_model
        self.transport = None
        if self.model != "gpt-6-astra":
            raise ValueError("Completion Compiler v2 requires OPENAI_MODEL=gpt-6-astra")

    async def propose(self, task, context, capabilities, effort, feedback, usage) -> Candidate:
        if not self.api_key:
            raise ModelUnavailable() from None
        if effort not in {"low", "medium", "high", "xhigh"}:
            raise ValueError("Unsupported compilation reasoning effort")
        usage.model = self.model
        usage.efforts.append(effort)
        payload = {
            "model": self.model,
            "store": False,
            "max_output_tokens": 8192,
            "reasoning": {"effort": effort},
            "input": [
                {"role": "system", "content": self.system},
                {
                    "role": "user",
                    "content": json.dumps({"task": task, "context": context,
                        "workspace_capabilities": capabilities, "validation_feedback_codes": feedback}),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "completion_candidate",
                    "strict": True,
                    "schema": self.schema,
                },
                "verbosity": "low",
            },
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=40, follow_redirects=False, transport=self.transport) as client:
                r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError):
            usage.complete = False
            raise ModelUnavailable() from None
        if not isinstance(data, dict):
            usage.complete = False
            raise InvalidCandidate() from None
        raw_usage = data.get("usage")
        if not isinstance(raw_usage, dict):
            usage.complete = False
        else:
            details = raw_usage.get("input_tokens_details")
            output_details = raw_usage.get("output_tokens_details")
            details = details if isinstance(details, dict) else {}
            output_details = output_details if isinstance(output_details, dict) else {}
            for dest, value in {
                "input_tokens": raw_usage.get("input_tokens"),
                "output_tokens": raw_usage.get("output_tokens"),
                "cached_input_tokens": details.get("cached_tokens", 0),
                "cache_write_tokens": details.get("cache_write_tokens", 0),
                "reasoning_tokens": output_details.get("reasoning_tokens", 0),
            }.items():
                if type(value) is int and value >= 0:
                    setattr(usage, dest, getattr(usage, dest) + value)
                else:
                    usage.complete = False
        try:
            if data.get("status") != "completed":
                raise ValueError()
            candidate = Candidate.model_validate(json.loads(data.get("output_text") or self._extract_text(data)))
            ids = [pc.id for pc in candidate.postconditions] + [x for intent in candidate.intents for x in intent.condition_ids]
            if any(not re.fullmatch(r"p[1-9][0-9]{0,2}", ident) for ident in ids):
                raise ValueError()
            for pc in candidate.postconditions:
                pc.description = f"Requested {pc.provider} outcome"
            return candidate
        except (ValueError, TypeError, RuntimeError, KeyError):
            raise InvalidCandidate() from None

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        for item in data.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") in {"output_text", "text"} and c.get("text"):
                        return c["text"]
        raise RuntimeError("Model response did not contain contract output")

    @staticmethod
    def _validate_compiled_selectors(contract: CompletionContract, registry=None) -> None:
        registry = registry or default_registry()
        for pc in contract.postconditions:
            if pc.provider != "unresolved":
                registry.require(pc.provider).compiler.validate_legacy_selector(pc)
