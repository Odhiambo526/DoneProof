from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from jsonschema import Draft202012Validator

from doneproof.compilation import ContractCompiler
from doneproof.compilation_models import SelectorCheck
from doneproof.compiler import CANDIDATE_SCHEMA
from doneproof.contract_analysis import analyze
from doneproof.intent import fast_candidate


class ReadyResolver:
    def __init__(self):
        self.calls = []

    async def capabilities(self, tenant):
        self.calls.append(("capabilities", tenant))
        return dict.fromkeys(["github", "gmail", "webhook"], "available")

    async def resolve(self, candidate, tenant, capabilities):
        self.calls.append(("resolve", tenant))
        return [SelectorCheck(condition_ids=[p.id], status="resolved", code="preflight_only")
                for p in candidate.postconditions], []


def run(coro):
    return asyncio.run(coro)


def compiler(settings, replies=(), effort="low"):
    resolver = ReadyResolver()
    service = ContractCompiler(replace(settings, openai_api_key="model-key-sentinel",
                                        compiler_reasoning_effort=effort), resolver)
    requests = []
    responses = iter(replies)
    async def model(request):
        requests.append(json.loads(request.content))
        reply = next(responses)
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": "provider-secret-sentinel"})
        data = reply.model_dump() if hasattr(reply, "model_dump") else reply
        return httpx.Response(200, json={"status": "completed", "output_text": json.dumps(data),
            "usage": {"input_tokens": 100, "output_tokens": 50,
                      "input_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 5},
                      "output_tokens_details": {"reasoning_tokens": 20}}})
    service.model.transport = httpx.MockTransport(model)
    return service, requests


def paraphrase_candidate():
    result = fast_candidate("Close issue #12 in acme/api")
    result.intents[0].source_text = "Mark issue #12 in acme/api as closed"
    return result


def test_candidate_schema_is_strict_and_valid():
    Draft202012Validator.check_schema(CANDIDATE_SCHEMA)
    def walk(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(CANDIDATE_SCHEMA)


@pytest.mark.parametrize("task", [
    "Close issue #12 in acme/api", "Reopen issue #19 in acme/api", "Merge PR #7 in acme/api",
    "Lock issue #12 in acme/api", "Unlock PR #9 in acme/api", "Assign issue #12 in acme/api to maya",
    'Add label "release ready" to PR #9 in acme/api', 'Rename issue #12 in acme/api to "Follow up"',
    "Verify PR #7 in acme/api is merged", 'Create issue in acme/api titled "Release; follow up"',
    'Create pull request in acme/api titled "Ship release" from "release" to "main"',
    'Send email to ana@example.com with subject "Q3 report" with attachment "report.pdf"',
    "Send Gmail draft msg17", "Check Gmail message msg5 is draft",
    'Verify Gmail message msg8 has attachment "invoice.pdf"',
    'Wait for webhook "refund.completed" from "erp" for object "order-9" with payload.status = "refunded"',
    "Close issue #12 in acme/api; Merge PR #7 in acme/api",
])
def test_fast_path_full_clauses_without_model(settings, task):
    service, requests = compiler(settings)
    result = run(service.compile(task, {}, "tenant-a"))
    assert result.status == "valid_contract", result.clarification_requirements
    assert requests == []
    assert result.usage.input_tokens == result.usage.output_tokens == 0
    assert result.contract_quality.evidence is False
    assert result.contract_quality.requires_registration
    assert result.stages[-1] == "final_contract"
    assert all(x.required for x in result.contract.postconditions)
    assert service.resolver.calls == [("capabilities", "tenant-a"), ("resolve", "tenant-a")]


@pytest.mark.parametrize("suffix", [" and email the customer", " unless the issue is urgent", " without notifying anyone",
                                     " and make sure the code is bug-free", "; deploy to production"])
def test_fast_path_does_not_ignore_unconsumed_requirements(suffix):
    assert fast_candidate("Close issue #12 in acme/api" + suffix) is None


@pytest.mark.parametrize("effort", ["low", "medium"])
def test_astra_ordinary_effort_usage_privacy_and_no_escalation(settings, effort):
    c = paraphrase_candidate()
    service, requests = compiler(settings, [c], effort)
    result = run(service.compile(c.intents[0].source_text, {"executor_claim": "I completed it", "repo": "acme/api"}, "a"))
    assert result.status == "valid_contract", result.clarification_requirements
    assert result.usage.efforts == [effort]
    assert result.usage.input_tokens == 100
    assert result.usage.cached_input_tokens == 10
    assert result.usage.cache_write_tokens == 5
    assert result.usage.output_tokens == 50
    assert result.usage.reasoning_tokens == 20
    assert not result.deterministic
    assert requests[0]["model"] == "gpt-6-astra"
    assert requests[0]["store"] is False
    assert requests[0]["max_output_tokens"] == 8192
    assert "executor_claim" not in json.dumps(requests)
    assert "model-key-sentinel" not in result.model_dump_json()
    assert "contract_quality" not in result.contract.model_dump()


def test_missing_transition_escalates_then_repairs(settings):
    broken, fixed = paraphrase_candidate(), paraphrase_candidate()
    broken.postconditions[0].require_change = False
    service, requests = compiler(settings, [broken, fixed])
    result = run(service.compile(fixed.intents[0].source_text, {}, "a"))
    assert result.status == "valid_contract"
    assert result.contract.postconditions[0].require_change
    assert result.usage.efforts == ["low", "high"]
    assert result.usage.escalation_reasons == ["static_validation_failed"]
    assert result.usage.input_tokens == 200
    assert "missing_transition" in json.loads(requests[1]["input"][1]["content"])["validation_feedback_codes"]


def test_ambiguity_escalates_to_xhigh_then_requires_clarification(settings):
    c = paraphrase_candidate()
    c.ambiguous = True
    service, requests = compiler(settings, [c, c, c])
    result = run(service.compile(c.intents[0].source_text, {}, "a"))
    assert result.status == "ambiguous_resource"
    assert result.contract is None
    assert result.usage.efforts == ["low", "high", "xhigh"]
    assert len(requests) == 3
    assert ("resolve", "a") not in service.resolver.calls


def test_model_cannot_invent_ids_or_use_confidence_as_authority(settings):
    c = paraphrase_candidate()
    c.postconditions[0].selector["number"] = 99
    service, requests = compiler(settings, [c])
    result = run(service.compile(c.intents[0].source_text, {}, "a"))
    assert result.status == "missing_identifier"
    assert result.contract is None
    assert len(requests) == 1  # Higher reasoning cannot supply a missing identifier.
    assert "ungrounded_identifier" in [x.code for x in result.clarification_requirements]
    assert result.contract_quality.confidence == 0


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_model_http_failure_does_not_escalate_or_expose_errors(settings, status):
    service, requests = compiler(settings, [status])
    result = run(service.compile("Mark issue #12 in acme/api as closed", {}, "a"))
    assert result.contract is None
    assert len(requests) == 1
    assert result.usage.complete is False
    assert "provider-secret-sentinel" not in result.model_dump_json()


def test_invalid_schema_bounded_repair(settings):
    service, requests = compiler(settings, [{"confidence": 1.0}] * 3)
    result = run(service.compile("Mark issue #12 in acme/api as closed", {}, "a"))
    assert result.contract is None
    assert result.usage.efforts == ["low", "high", "xhigh"]
    assert len(requests) == 3


def test_pipeline_deadline_is_fail_closed(settings):
    service, _ = compiler(settings)
    async def slow(tenant):
        await asyncio.sleep(10)
    service.resolver.capabilities = slow
    service.deadline_seconds = 0.01
    result = run(service.compile("Close issue #12 in acme/api", {}, "a"))
    assert result.contract is None
    assert result.clarification_requirements[0].code == "compilation_deadline"


@pytest.mark.parametrize("task,context", [
    ("Close issue #12 in acme/api", {"access_token": "secret-sentinel"}),
    ("Use Bearer secret-sentinel", {}),
    ("Use api_key=secret-sentinel", {}),
])
def test_sensitive_input_is_not_sent_stored_or_echoed(settings, task, context):
    service, requests = compiler(settings)
    result = run(service.compile(task, context, "a"))
    assert result.contract is None
    assert "secret-sentinel" not in result.model_dump_json()
    assert requests == []
    assert service.resolver.calls == []


def codes(c, task=None, context=None):
    return {x.code for x in analyze(c, task or c.intents[0].source_text, context or {})}


def test_static_duplicate_and_contradictory_conditions():
    c = fast_candidate("Close issue #12 in acme/api; Close issue #12 in acme/api")
    assert "duplicate_conditions" in codes(c, "Close issue #12 in acme/api; Close issue #12 in acme/api")
    c = fast_candidate("Close issue #12 in acme/api; Reopen issue #12 in acme/api")
    assert "contradictory_predicates" in codes(c, "Close issue #12 in acme/api; Reopen issue #12 in acme/api")


@pytest.mark.parametrize("selector", [
    {"repo": "acme/api", "kind": "issue", "number": True},
    {"repo": "https://example.com/acme/api", "kind": "issue", "number": 12},
    {"repo": "acme/api", "kind": "issue", "number": 12, "title": "unrelated"},
    {"repo": "acme/api", "kind": "issue", "head_ref": "release"},
])
def test_static_impossible_selectors(selector):
    c = paraphrase_candidate()
    c.postconditions[0].selector = selector
    assert "impossible_selector" in codes(c)


def test_static_unsafe_discovery():
    c = fast_candidate('Send email to ana@example.com with subject "Report"')
    for pc in c.postconditions:
        pc.selector.pop("subject", None)
    assert "unsafe_discovery" in codes(c)


@pytest.mark.parametrize("op,path,value", [
    ("exists", "", None), ("not_exists", "unknown_field", None), ("contains_all", "labels", []),
    ("contains", "state", ""), ("eq", "merged", True), ("eq", "state", "done"),
    ("gte", "title", 1), ("eq", "locked", 1),
])
def test_static_meaningless_or_unsupported_predicates(op, path, value):
    c = paraphrase_candidate()
    p = c.postconditions[0].predicate
    p.op, p.path, p.expected = op, path, value
    assert "meaningless_predicate" in codes(c)


def test_transition_mode_cannot_be_laundered_as_state():
    c = paraphrase_candidate()
    c.intents[0].mode = "state"
    c.postconditions[0].require_change = False
    assert "missing_transition" in codes(c)


def test_incomplete_intent_and_optional_outcomes_rejected():
    c = paraphrase_candidate()
    assert "incomplete_intent" in codes(c, c.intents[0].source_text + " and send the invoice")
    c.postconditions[0].required = False
    assert "over_broad_postcondition" in codes(c)


def test_model_cannot_substitute_other_expected_outcome():
    c = paraphrase_candidate()
    c.postconditions[0].predicate.path = "title"
    c.postconditions[0].predicate.expected = "invented business goal"
    assert "over_broad_postcondition" in codes(c)


def test_ordinary_model_concurrency_is_bounded_across_requests(settings):
    service, _ = compiler(settings)
    active, peak = 0, 0
    async def model(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(.01)
        active -= 1
        return httpx.Response(200, json={"status": "completed", "output_text": paraphrase_candidate().model_dump_json(),
                                        "usage": {"input_tokens": 10, "output_tokens": 10}})
    service.model.transport = httpx.MockTransport(model)
    async def many():
        return await asyncio.gather(*(service.compile("Mark issue #12 in acme/api as closed", {}, "a") for _ in range(8)))
    results = run(many())
    assert all(r.status == "valid_contract" for r in results)
    assert peak == 2


def test_typed_context_fast_path_and_literal_normalization(settings):
    service, requests = compiler(settings)
    result = run(service.compile("Please close issue #12", {"repo": "acme/api", "executor_claim": "closed"}, "a"))
    assert result.status == "valid_contract"
    assert result.contract.postconditions[0].selector["repo"] == "acme/api"
    assert not requests


def test_model_null_selector_fields_are_normalized():
    c = paraphrase_candidate()
    c.postconditions[0].selector.update({"message_id": None, "subject": None, "title": None})
    assert codes(c) == set()


def test_model_cannot_copy_identifier_from_other_intent():
    c = fast_candidate("Close issue #12 in acme/api; Reopen issue #99 in acme/api")
    c.postconditions[0].selector["number"] = 99
    assert "ungrounded_identifier" in codes(c, "Close issue #12 in acme/api; Reopen issue #99 in acme/api")


def test_model_cannot_drop_attachment_on_noncanonical_send():
    c = fast_candidate('Send email to ana@example.com with subject "Report"')
    c.intents[0].source_text = 'Email ana@example.com a message whose subject is "Report" with attachment "report.pdf"'
    assert "over_broad_postcondition" in codes(c)
