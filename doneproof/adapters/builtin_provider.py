"""Compatibility hooks owned by the three shipped provider implementations."""
import re
from dataclasses import replace

from ..provider_sdk import ProviderDefinition
from . import builtin_analysis as analysis
from .builtin_grammar import REPO, parse_clause
from .builtin_legacy_compiler import LegacyCompiler
from .builtin_oauth import BuiltinOAuthProvider


class BuiltinCompiler:
    def __init__(self, provider):
        self.provider = provider

    def parse_clause(self, clause, context):
        parsed = parse_clause(clause)
        if parsed is None and context:
            bound = clause
            repo = context.get("repo")
            if isinstance(repo, str) and re.fullmatch(REPO, repo):
                bound = re.sub(r"(?i)((?:issue|PR|pull request) #[0-9]+)(?![0-9]| in )", r"\g<1> in " + repo, bound)
            if re.fullmatch(r"(?i)Send (?:an? )?(?:email|message)", bound):
                subject, to = context.get("subject"), context.get("to")
                if all(isinstance(x, str) and not re.search(r'["\n\r]', x) for x in (subject, to)):
                    bound = f'Send email to {to} with subject "{subject}"'
            parsed = parse_clause(bound)
        return parsed if parsed and parsed[0] == self.provider else None

    def analyze_condition(self, pc, task, context):
        return analysis.analyze_condition(pc, task, context)

    def analyze_intent(self, intent, targets):
        return analysis.analyze_intent(intent, targets)

    def validate_legacy_selector(self, pc):
        from types import SimpleNamespace
        LegacyCompiler._validate_compiled_selectors(SimpleNamespace(postconditions=[pc]))


def schemas(provider):
    types = {str: "string", int: "integer", bool: "boolean", list: "array"}
    fields = {name: {"type": [types[kind], "null"], **({"items": {"type": "string"}} if kind is list else {})}
              for name, kind in analysis.FIELDS[provider].items()}
    if provider == "webhook":
        fields["payload"] = {"type": "object"}
    selectors = {key: {"type": "integer" if key == "number" else "string"}
                 for key in sorted(analysis.SELECTORS[provider])}
    selectors["created_after"] = {"type": "string"}
    if "kind" in selectors:
        selectors["kind"]["enum"] = ["issue", "pull_request"]
    if "location" in selectors:
        selectors["location"]["enum"] = ["sent", "draft", "other"]
    return ({"type": "object", "properties": fields, "additionalProperties": False},
            {"type": "object", "properties": selectors, "additionalProperties": False})


def legacy_credentials(provider, settings):
    imports = dict(settings.gmail_tokens) if provider == "gmail" else {}
    token = settings.gmail_access_token if provider == "gmail" else settings.github_token
    if token:
        tenants = set(settings.api_keys.values()) | set(settings.connection_admin_keys.values())
        tenant = settings.legacy_connection_tenant
        if not tenant:
            tenant = next(iter(tenants)) if len(tenants) == 1 else ("default" if not tenants else None)
        if not tenant:
            raise RuntimeError("Global legacy tokens require DONEPROOF_LEGACY_CONNECTION_TENANT")
        imports.setdefault(tenant, token)
    return [(tenant, provider, token) for tenant, token in imports.items()]


def validate_configuration(provider, settings):
    identifier = settings.google_client_id if provider == "gmail" else settings.github_client_id
    if identifier and not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", identifier):
        raise RuntimeError("Invalid OAuth client identifier")
    if provider == "github" and settings.github_app_slug and not re.fullmatch(r"[a-zA-Z0-9-]{1,100}", settings.github_app_slug):
        raise RuntimeError("Invalid GitHub App slug")


def definition(manifest, factory):
    name = manifest["provider_id"]
    evidence, selector = schemas(name)
    manifest.update(version="1.0.0", evidence_schema=evidence, selector_schema=selector,
                    supported_predicates=tuple(sorted({"eq", "neq", "exists", "not_exists", "contains", "contains_all", "gte", "lte"})),
                    baseline_support=True, transition_support=True,
                    context_fields=tuple(sorted(analysis.BINDINGS & (analysis.SELECTORS[name]
                        | set(analysis.FIELDS[name]) | {"assignee", "label", "attachment_name"}))))
    kwargs = {}
    if manifest["authentication"]["mode"] == "managed_oauth":
        kwargs = {"connection_factory": BuiltinOAuthProvider,
                  "legacy_credentials": lambda settings: legacy_credentials(name, settings),
                  "validate_configuration": lambda settings: validate_configuration(name, settings),
                  "installation_url": lambda settings: "https://github.com/apps/" + settings.github_app_slug + "/installations/new"
                      if name == "github" and settings.github_app_slug else None}
    else:
        kwargs = {"capability": lambda tenant, settings: "available" if any(
            source.tenant_id == tenant for source in settings.webhook_sources.values()) else "configuration_required",
            "event_selector_allowed": lambda tenant, selector, settings: bool(
                (source := settings.webhook_sources.get(selector.get("source"))) and source.tenant_id == tenant)}
    return ProviderDefinition(manifest, factory, BuiltinCompiler(name), **kwargs)


def gmail_settings(runtime):
    return replace(runtime.settings, gmail_access_token=None,
                   gmail_tokens={runtime.tenant_id: runtime.credentials["access_token"]})


def builtin_definitions():
    from .github import provider_definition as github
    from .gmail import provider_definition as gmail
    from .webhook import provider_definition as webhook
    return github(), gmail(), webhook()
