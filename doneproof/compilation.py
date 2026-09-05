"""Fail-closed completion contract compilation pipeline."""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone

from .compilation_models import CompilationResult, CompilationUsage, ContractQuality, issue
from .compiler import AstraCompiler, InvalidCandidate, ModelUnavailable
from .contract_analysis import analyze, consistency_problems, safe_context, sensitive
from .domain import CompletionContract
from .intent import fast_candidate
from .provider_registry import default_registry

STAGES = ["intent_decomposition", "capability_resolution", "candidate_contract", "static_validation",
          "selector_resolution", "ambiguity_assessment", "final_contract"]
REPAIRABLE = {"invalid_candidate", "incomplete_intent", "duplicate_conditions", "contradictory_predicates",
              "impossible_selector", "meaningless_predicate", "missing_transition", "over_broad_postcondition"}


class ContractCompiler:
    def __init__(self, settings, resolver):
        self.registry = getattr(resolver, "registry", None) or default_registry()
        self.model = AstraCompiler(settings, self.registry)
        self.resolver = resolver
        self.ordinary_effort = settings.compiler_reasoning_effort
        if self.ordinary_effort not in {"low", "medium"}:
            raise ValueError("DONEPROOF_COMPILER_REASONING_EFFORT must be low or medium")
        self.model_limit = asyncio.Semaphore(2)
        self.deadline_seconds = 90

    async def compile(self, task, context, tenant, task_started_at=None):
        started = time.perf_counter()
        usage = CompilationUsage()
        try:
            async with asyncio.timeout(self.deadline_seconds):
                result = await self._compile(task, context, tenant, task_started_at, usage)
        except TimeoutError:
            usage.complete = not usage.efforts
            result = self._result([issue("compilation_deadline")], usage=usage, deterministic=not usage.efforts)
        result.latency_ms = round((time.perf_counter() - started) * 1000, 3)
        return result

    async def _compile(self, task, context, tenant, task_started_at, usage):
        if sensitive(task, context):
            return self._result([issue("sensitive_input")])
        context = safe_context(context, self.registry)
        candidate = fast_candidate(task, context, self.registry)
        deterministic = candidate is not None
        # Obvious unsupported integrations need no model call and no network discovery.
        unsupported = {"slack", "notion", "salesforce", "jira", "asana", "trello", "outlook", "exchange"} - {
            d.manifest.provider_id for d in self.registry}
        if candidate is None and (any(re.search(r"(?i)\b" + name + r"\b", task) for name in unsupported)
                                  or re.search(r"(?i)arbitrary.url", task)):
            return self._result([issue("unsupported_provider", "unsupported_provider")])
        capabilities = await self.resolver.capabilities(tenant)
        problems = []
        if deterministic:
            problems = analyze(candidate, task, context, self.registry)
        else:
            for attempt, effort in enumerate([self.ordinary_effort, "high", "xhigh"]):
                if attempt:
                    reason = "ambiguous_intent" if candidate and candidate.ambiguous else "static_validation_failed"
                    usage.escalation_reasons.append(reason)
                try:
                    async with self.model_limit:
                        candidate = await self.model.propose(task, context, capabilities, effort,
                            sorted({p.code for p in problems}), usage)
                    problems = analyze(candidate, task, context, self.registry)
                except ModelUnavailable:
                    return self._result([self._unparsed_issue(task)], usage=usage, deterministic=False)
                except InvalidCandidate:
                    candidate = None
                    problems = [issue("invalid_candidate")]
                if candidate and candidate.ambiguous:
                    problems.append(issue("ambiguous_resource", "ambiguous_resource"))
                if not problems or not all(p.code in REPAIRABLE or p.code == "ambiguous_resource" for p in problems):
                    break
        if problems:
            return self._result(problems, usage=usage, deterministic=deterministic, stages=STAGES[:4])
        checks, problems = await self.resolver.resolve(candidate, tenant, capabilities)
        # Different supplied selectors can resolve to the same authoritative resource.
        problems.extend(consistency_problems(candidate))
        if problems:
            return self._result(problems, usage=usage, deterministic=deterministic, checks=checks, stages=STAGES[:6])
        now = datetime.now(timezone.utc)
        boundary = task_started_at or now
        if boundary.tzinfo is None:
            boundary = boundary.replace(tzinfo=timezone.utc)
        contract = CompletionContract(task=task, postconditions=candidate.postconditions,
            task_started_at=boundary.astimezone(timezone.utc), created_at=now)
        warnings = [issue("preflight_only")]
        for provider in sorted({pc.provider for pc in candidate.postconditions}):
            warnings.extend(getattr(self.registry.require(provider).compiler, "compilation_warnings", lambda: [])())
        if any(c.status == "deferred" for c in checks):
            warnings.append(issue("future_discovery"))
        if not deterministic:
            warnings.append(issue("model_interpretation"))
        return CompilationResult(status="valid_contract", contract=contract,
            contract_quality=ContractQuality(confidence=0.95 if deterministic else 0.8, warnings=warnings),
            selector_checks=checks, stages=STAGES, deterministic=deterministic, usage=usage)

    @staticmethod
    def _unparsed_issue(task):
        if re.search(r"(?i)\b(read by|understood|happy|satisfied|approve|approval|bug.free|email body|message body)\b", task):
            return issue("unsupported_outcome")
        if re.fullmatch(r"(?i)(?:close|merge|reopen|assign|send|create|verify|check)\s+(?:the\s+)?(?:issue|PR|pull request|email|message|draft)", task.strip().rstrip(".")):
            return issue("missing_identifier", "missing_identifier", fields=["resource_identifier", "desired_outcome"])
        return issue("model_unavailable")

    @staticmethod
    def _result(problems, *, usage=None, deterministic=True, checks=None, stages=None):
        # Whole-task failure: never publish a partial executable contract.
        priority = ["unsupported_provider", "missing_identifier", "ambiguous_resource", "unverifiable_outcome"]
        status = next(s for s in priority if any(p.category == s for p in problems))
        return CompilationResult(status=status, clarification_requirements=problems,
            contract_quality=ContractQuality(confidence=0, warnings=problems),
            selector_checks=checks or [], stages=stages or STAGES[:1],
            deterministic=deterministic, usage=usage or CompilationUsage())
