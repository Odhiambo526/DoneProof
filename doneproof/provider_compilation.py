"""Conservative compiler hooks for an SDK provider with exact selectors.

Providers can subclass for their own full-clause grammar and additional semantics.
The default never guesses a task interpretation or authorizes broad discovery.
"""
import json
import re

from jsonschema import Draft202012Validator

from .compilation_models import issue
from .security import sanitize


class SchemaCompiler:
    def __init__(self, manifest):
        from .provider_sdk import ProviderManifest
        self._manifest = ProviderManifest.model_validate(manifest).model_dump_json()

    @property
    def manifest(self):
        from .provider_sdk import ProviderManifest
        return ProviderManifest.model_validate_json(self._manifest)

    def parse_clause(self, clause, context):
        spec = self.manifest
        pattern = (r"(Verify|Change) " + re.escape(spec.provider_id) + r" (" +
                   "|".join(re.escape(resource) for resource in spec.resource_types) +
                   r") (.+) has ([A-Za-z_][A-Za-z0-9_.]*) = (.+)")
        match = re.fullmatch(pattern, clause)
        if not match:
            return None
        try:
            identifier, expected = json.loads(match[3]), json.loads(match[5])
        except ValueError:
            return None
        if type(identifier) not in (str, int) or type(expected) not in (str, int, float, bool, list):
            return None
        return (spec.provider_id, {spec.discovery.identity_field: identifier}, [("eq", match[4], expected)],
                "transition" if match[1] == "Change" else "state")

    def analyze_intent(self, intent, targets):
        # Provider-specific verbs need a deterministic interpretation before they
        # can be safely certified, even when a model emits a schema-valid candidate.
        parsed = self.parse_clause(intent.source_text, {})
        return [] if parsed else [issue("unsupported_outcome", ids=intent.condition_ids)]

    def analyze_condition(self, pc, task, context):
        from .contract_analysis import grounded, sensitive
        spec, problems = self.manifest, []
        def add(code, category="unverifiable_outcome", fields=()):
            problems.append(issue(code, category, ids=[pc.id], fields=fields))
        selector = {k: v for k, v in pc.selector.items() if v is not None}
        pc.selector = selector
        if not pc.required:
            add("over_broad_postcondition")
        if not Draft202012Validator(spec.selector_schema).is_valid(selector) or sanitize(selector) != selector:
            add("impossible_selector")
        for key, value in selector.items():
            if type(value) not in (str, int) or not grounded(value, key, task, context):
                add("ungrounded_identifier", "missing_identifier", [key])
        if spec.discovery.is_discovery(selector):
            # Broader discovery requires a provider-owned static-analysis hook.
            add("unsafe_discovery", "missing_identifier", [spec.discovery.identity_field])
        p = pc.predicate
        schema = spec.evidence_schema
        for part in p.path.split("."):
            schema = schema.get("properties", {}).get(part, {})
        if not schema or p.op in {"exists", "not_exists"} or p.expected is None:
            add("meaningless_predicate")
        elif p.op in {"eq", "neq"}:
            if not Draft202012Validator(schema).is_valid(p.expected):
                add("meaningless_predicate")
        elif p.op in {"contains", "contains_all"}:
            values = p.expected if p.op == "contains_all" else [p.expected]
            if (schema.get("type") != "array" or not isinstance(values, list) or not values
                    or any(not Draft202012Validator(schema.get("items", False)).is_valid(v) for v in values)):
                add("meaningless_predicate")
        elif p.op in {"gte", "lte"} and (schema.get("type") not in {"integer", "number"} or type(p.expected) not in (int, float)):
            add("meaningless_predicate")
        values = p.expected if isinstance(p.expected, list) else [p.expected]
        if any(not grounded(v, p.path, task, context) for v in values):
            add("over_broad_postcondition")
        if sensitive("", {"expected": p.expected}) or sanitize({p.path: p.expected}) != {p.path: p.expected}:
            add("meaningless_predicate")
        if pc.require_change and p.path == spec.discovery.identity_field:
            add("meaningless_predicate")
        return problems

    def validate_legacy_selector(self, pc):
        if not Draft202012Validator(self.manifest.selector_schema).is_valid(pc.selector):
            raise ValueError("Compiled provider selector is invalid")
