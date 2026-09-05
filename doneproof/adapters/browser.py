"""Lower-assurance UI observations for approved workflows without authoritative APIs."""
import asyncio
import importlib.util
import re
from uuid import uuid4

from ..browser_artifacts import BrowserArtifacts
from ..browser_models import BrowserProvenance
from ..browser_network import BrowserUnavailable
from ..browser_policy import ID, REVISION, BrowserChecks
from ..browser_runner import ChromiumObserver
from ..compilation_models import issue
from ..provider_compilation import SchemaCompiler
from ..provider_sdk import ProviderDefinition
from .base import ProviderAdapter, ProviderObservation


def valid_selector(selector):
    return (set(selector) == {"check_id", "revision"}
            and isinstance(selector["check_id"], str) and re.fullmatch(ID, selector["check_id"])
            and isinstance(selector["revision"], str) and re.fullmatch(REVISION, selector["revision"]))


class BrowserCompiler(SchemaCompiler):
    def compilation_warnings(self):
        return [issue("browser_lower_assurance")]

    def parse_clause(self, clause, context):
        # Explicit browser opt-in only; the compiler never falls back from an API task.
        match = re.fullmatch(r'(Verify|Change) browser check "([a-z][a-z0-9_-]{0,63})" at revision "([a-f0-9]{64})" matches', clause)
        if match is None:
            return None
        return ("browser", {"check_id": match[2], "revision": match[3]}, [("eq", "matched", True)],
                "transition" if match[1] == "Change" else "state")

    def analyze_condition(self, pc, task, context):
        parsed = self.parse_clause(task.strip().rstrip("."), context)
        # Individual clauses are analyzed again by analyze_intent for compound tasks.
        valid = (valid_selector(pc.selector) and pc.predicate.op == "eq" and pc.predicate.path == "matched"
                 and pc.predicate.expected is True and pc.required)
        if not valid:
            return [issue("meaningless_predicate", ids=[pc.id])]
        if parsed and (parsed[1] != pc.selector or (parsed[3] == "transition") != pc.require_change):
            return [issue("over_broad_postcondition", ids=[pc.id])]
        return []

    def analyze_intent(self, intent, targets):
        parsed = self.parse_clause(intent.source_text, {})
        if (not parsed or len(targets) != 1 or parsed[1] != targets[0].selector
                or parsed[3] != intent.mode or targets[0].require_change != (intent.mode == "transition")):
            return [issue("unsupported_outcome", ids=intent.condition_ids)]
        return []


class BrowserAdapter(ProviderAdapter):
    def __init__(self, runtime, *, observer=None):
        self.checks = BrowserChecks(runtime.settings.browser_checks)
        self.artifacts = BrowserArtifacts(runtime.store, runtime.settings)
        self.observer = observer or ChromiumObserver()

    def default_provenance(self):
        return BrowserProvenance()

    @staticmethod
    def validate_postcondition(pc):
        return (valid_selector(pc.selector) and pc.predicate.op == "eq"
                and pc.predicate.path == "matched" and pc.predicate.expected is True)

    def observation_is_current(self, authority, tenant_id):
        if not authority or authority.get("mode") != "browser" or authority.get("tenant") != tenant_id:
            return False
        check = self.checks.get(tenant_id, authority.get("check_id"))
        return bool(check and check.enabled and not check.api_required and check.revision == authority.get("revision"))

    async def observe(self, selector, context):
        provenance = BrowserProvenance()
        authority = None
        def unknown(code):
            provenance.outcome = code
            return ProviderObservation(None, indeterminate=True, authority=authority, provenance=provenance,
                note="Browser UI verification is lower assurance. Independent observation was inconclusive: " + code + ".")

        if not valid_selector(selector):
            return unknown("invalid_condition")
        check = self.checks.get(context.tenant_id, selector["check_id"])
        if not check or not check.enabled:
            return unknown("not_configured")
        provenance.check_revision = check.revision
        authority = {"mode": "browser", "tenant": context.tenant_id, "check_id": selector["check_id"], "revision": check.revision}
        if check.api_required:
            return unknown("api_required")
        if selector["revision"] != check.revision:
            return unknown("policy_changed")
        if not self.artifacts.vault.available:
            return unknown("screenshot_unavailable")
        provenance.session_id = "bo_" + uuid4().hex
        try:
            async with asyncio.timeout(12):
                capture = await self.observer.capture(check)
            provenance.fresh_context = True
            if capture.state not in check.states or capture.samples != 3:
                return unknown("ambiguous_ui")
            # Preflight cannot become a persisted verification artifact or signed evidence.
            if not context.contract_id.startswith("preflight_"):
                provenance.screenshot = self.artifacts.save(context, capture.png)
            provenance.recognized_state = capture.state
            provenance.samples = capture.samples
            provenance.outcome = "recognized"
            return ProviderObservation({"check_id": selector["check_id"], "revision": check.revision,
                "matched": capture.state == check.success_state}, source_url=check.url, authority=authority,
                provenance=provenance, note="Independent browser UI observation; lower assurance than an authoritative API.")
        except BrowserUnavailable as exc:
            provenance.fresh_context = True
            return unknown(exc.code)
        except TimeoutError:
            return unknown("deadline")
        except Exception:
            # Browser errors may include page contents, URLs and launch environment. Never log them.
            return unknown("browser_unavailable")


def capability(tenant, settings):
    configured = BrowserChecks(settings.browser_checks).available(tenant)
    return "available" if (configured and settings.connection_active_key and settings.connection_encryption_keys
                           and importlib.util.find_spec("playwright")) else "configuration_required"


def provider_definition():
    manifest = {
        "provider_id": "browser", "version": "1.0.0", "display_name": "Browser UI",
        "description": "Independent UI observations with lower assurance than authoritative APIs. Only approved tenant checks without API coverage are eligible.",
        "resource_types": ("check",),
        "selector_schema": {"type": "object", "properties": {"check_id": {"type": "string", "pattern": ID},
            "revision": {"type": "string", "pattern": REVISION}}, "required": ["check_id", "revision"], "additionalProperties": False},
        "evidence_schema": {"type": "object", "properties": {"check_id": {"type": "string", "pattern": ID},
            "revision": {"type": "string", "pattern": REVISION}, "matched": {"type": "boolean"}},
            "required": ["check_id", "revision", "matched"], "additionalProperties": False},
        "supported_predicates": ("eq",), "discovery": {"supported": False, "identity_field": "check_id",
            "identity_schema": {"type": "string", "pattern": ID}},
        "baseline_support": True, "transition_support": True,
        "authentication": {"mode": "none", "requirements": ("operator-approved tenant check", "fresh unauthenticated verifier session")},
        "rate_limit": {"concurrency": 2, "preflight_concurrency": 1, "attempts": 1, "base_seconds": 1, "cap_seconds": 1},
        "evidence_sensitivity": "restricted", "context_fields": (),
        "compiler_instructions": 'Only explicit clauses: Verify browser check "ID" at revision "SHA256" matches. '
            'Use IDs and revisions returned by GET /v1/browser/checks. Predicate must be matched eq true. '
            'Never substitute browser checks for Gmail, GitHub, signed webhook or other authoritative API evidence. '
            'Browser evidence is lower assurance; never invent checks, revisions, URLs, scripts or browser state.',
    }
    return ProviderDefinition(manifest, BrowserAdapter, BrowserCompiler(manifest), capability=capability,
                              validate_configuration=lambda settings: BrowserChecks(settings.browser_checks),
                              admit_condition=BrowserAdapter.validate_postcondition)
