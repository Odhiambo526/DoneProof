from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Any

from .adapters.base import ObservationContext, ProviderAdapter
from .domain import (
    CompletionContract,
    ConditionResult,
    ConditionStatus,
    Evidence,
    Verdict,
    VerificationReceipt,
    VerificationSummary,
)
from .pipeline import ObservationRecord
from .predicates import evaluate
from .provider_registry import default_registry
from .recovery_models import RecoveryInfo
from .remediation import GUIDANCE_FIELDS, contains_guidance, remediation_for
from .retries import TransientObservationError, durable_observation, transient_exception
from .security import sanitize
from .signing import ReceiptSigner

logger = logging.getLogger("doneproof.verification")


class VerificationEngine:
    def __init__(self, adapters: dict[str, ProviderAdapter], signer: ReceiptSigner,
                 timeout_seconds: float = 15.0, provider_concurrency: dict[str, int] | None = None, registry=None,
                 provider_binding_check=None):
        self.adapters = adapters
        self.signer = signer
        self.timeout_seconds = timeout_seconds
        self.registry = registry or default_registry()
        self.provider_binding_check = provider_binding_check
        self.provider_concurrency = provider_concurrency or self.registry.concurrency()

    def selector_for(self, pc, contract):
        selector = dict(pc.selector)
        definition = self.registry.get(pc.provider)
        if definition:
            discovery = definition.manifest.discovery
            if discovery.boundary_field and discovery.is_discovery(selector):
                selector[discovery.boundary_field] = contract.task_started_at.isoformat()
        return selector

    async def observe(self, pc, contract, tenant_id, *, capture=False, durable=False) -> ObservationRecord:
        started = time.perf_counter()
        adapter = self.adapters.get(pc.provider)
        if adapter is None:
            return ObservationRecord(indeterminate=True, note="Provider is not available in this deployment.")
        if self.provider_binding_check and not self.provider_binding_check(tenant_id, contract.id, pc.provider):
            return ObservationRecord(indeterminate=True, note="The registered provider definition changed; register a new run.")
        context = ObservationContext(tenant_id=tenant_id, contract_id=contract.id,
            task_started_at=contract.task_started_at.isoformat(), condition_id=pc.id,
            require_connection_binding=pc.require_change, capture_connection_binding=capture)
        token = durable_observation.set(durable)
        try:
            observation = await asyncio.wait_for(
                adapter.observe(self.selector_for(pc, contract), context), timeout=self.timeout_seconds)
            definition = self.registry.get(pc.provider)
            return ObservationRecord(state=observation.state, source_url=observation.source_url,
                note=observation.note, indeterminate=observation.indeterminate, authority=observation.authority,
                redacted_paths=list(definition.manifest.sensitive_paths) if definition else [],
                latency_ms=round((time.perf_counter() - started) * 1000, 2))
        except TransientObservationError:
            if durable:
                raise
            return ObservationRecord(indeterminate=True, note="Provider verification was unavailable or returned an invalid response.")
        except asyncio.TimeoutError:
            if durable:
                raise TransientObservationError("provider_timeout") from None
            logger.warning("verification_timeout provider=%s contract_id=%s condition_id=%s", pc.provider, contract.id, pc.id)
            return ObservationRecord(indeterminate=True, note="Verification timed out before authoritative state was established.")
        except Exception as exc:
            if durable and transient_exception(exc):
                raise TransientObservationError() from None
            logger.warning("verification_provider_error provider=%s contract_id=%s condition_id=%s error_type=%s",
                           pc.provider, contract.id, pc.id, type(exc).__name__)
            return ObservationRecord(indeterminate=True, note="Provider verification was unavailable or returned an invalid response.")
        finally:
            durable_observation.reset(token)

    def observation_is_current(self, pc, observation, tenant_id):
        adapter = self.adapters.get(pc.provider)
        return adapter is None or adapter.observation_is_current(observation.authority, tenant_id)

    def evaluate_observation(self, pc, contract, observation: ObservationRecord) -> ConditionResult:
        definition = self.registry.get(pc.provider)
        if definition:
            spec = definition.manifest
            observation = observation.model_copy(update={
                "redacted_paths": sorted(set(observation.redacted_paths) | set(spec.sensitive_paths))})
            if (pc.predicate.op not in spec.supported_predicates
                    or pc.require_change and not spec.transition_support):
                observation = observation.model_copy(update={"state": None, "indeterminate": True,
                    "note": "The provider does not support the requested predicate or transition."})
        observation = observation.checkpoint()
        if (contains_guidance(observation.state)
                or any(part in GUIDANCE_FIELDS for part in pc.predicate.path.split("."))):
            status, reason, observed = (ConditionStatus.UNKNOWN,
                "DoneProof remediation and receipts are guidance, never authoritative outcome evidence.", None)
        elif observation.predicate_is_redacted(pc.predicate.path):
            status, reason, observed = (ConditionStatus.UNKNOWN,
                "The predicate depends on sensitive state that cannot be retained as evidence.", None)
        elif observation.indeterminate:
            status, reason, observed = (ConditionStatus.UNKNOWN,
                observation.note or "Authoritative state could not be established.", observation.state)
        else:
            status, reason, observed = evaluate(observation.state, pc.predicate)
        return ConditionResult(id=pc.id, description=pc.description, required=pc.required,
            status=status, predicate=pc.predicate,
            evidence=Evidence(provider=pc.provider, selector=sanitize(self.selector_for(pc, contract)),
                observed=sanitize(observed), source_url=observation.source_url,
                note=observation.note, fetched_at=observation.fetched_at),
            reason=reason, transition_required=pc.require_change, latency_ms=observation.latency_ms)

    async def _verify_one(self, pc, contract: CompletionContract, tenant_id: str, capture=False) -> ConditionResult:
        observed = await self.observe(pc, contract, tenant_id, capture=capture)
        return self.evaluate_observation(pc, contract, observed)

    def _result_unknown(self, pc, selector: dict[str, Any], reason: str, started: float) -> ConditionResult:
        return ConditionResult(id=pc.id, description=pc.description, required=pc.required,
            status=ConditionStatus.UNKNOWN, predicate=pc.predicate,
            evidence=Evidence(provider=pc.provider, selector=selector, note=reason),
            reason=reason, transition_required=pc.require_change,
            latency_ms=round((time.perf_counter() - started) * 1000, 2))

    async def _conditions(self, targets, contract, tenant_id, capture=False):
        limits = {name: asyncio.Semaphore(limit) for name, limit in self.provider_concurrency.items()}
        async def one(pc):
            async with limits.get(pc.provider, limits["unresolved"]):
                return await self._verify_one(pc, contract, tenant_id, capture=capture)
        return list(await asyncio.gather(*(one(pc) for pc in targets)))

    async def snapshot(self, contract: CompletionContract, tenant_id: str = "default") -> list[ConditionResult]:
        return await self._conditions([pc for pc in contract.postconditions if pc.require_change],
                                      contract, tenant_id, capture=True)

    def evaluate_transitions(self, contract, results, assurance_level="submitted", baselines=None):
        results = [result.model_copy(deep=True) for result in results]

        baseline_map = baselines or {}
        pc_by_id = {pc.id: pc for pc in contract.postconditions}
        for result in results:
            pc = pc_by_id[result.id]
            if not pc.require_change:
                continue
            baseline = baseline_map.get(pc.id)
            if assurance_level != "registered" or baseline is None:
                result.status = ConditionStatus.UNKNOWN
                result.reason = "This outcome requires transition proof from a pre-execution registered baseline."
                result.evidence.note = result.reason
                continue
            result.baseline_status = baseline.status
            result.baseline_observed = baseline.evidence.observed
            if baseline.status == ConditionStatus.UNKNOWN:
                result.status = ConditionStatus.UNKNOWN
                result.reason = "Pre-execution state was unknown, so the requested transition cannot be proven."
                result.evidence.note = result.reason
            elif result.status == ConditionStatus.PASS and baseline.status == ConditionStatus.PASS:
                result.status = ConditionStatus.FAIL
                result.reason = "Desired state already satisfied the condition before execution; no requested transition was proven."
                result.evidence.note = result.reason
            elif result.status == ConditionStatus.PASS and baseline.status == ConditionStatus.FAIL:
                result.reason = "Transition verified: pre-execution state did not satisfy the condition and post-execution state does."
                result.evidence.note = (result.evidence.note + " " if result.evidence.note else "") + result.reason
        return results


    def build_receipt(self, contract, results, *, assurance_level="submitted", duration_ms=0.0,
                      receipt_id: str | None = None, verified_at: datetime | None = None):
        payload = json.dumps(contract.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode()
        fields = {}
        if receipt_id is not None:
            fields["receipt_id"] = receipt_id
        if verified_at is not None:
            fields["verified_at"] = verified_at
        receipt = VerificationReceipt(assurance_level=assurance_level, contract_id=contract.id,
            contract_hash=hashlib.sha256(payload).hexdigest(), task=contract.task,
            verdict=self._verdict(results), summary=self._summary(results), results=results,
            duration_ms=round(duration_ms, 2), **fields)
        receipt.schema_version = "1.1"
        receipt.remediation = remediation_for(results)
        receipt.recovery = RecoveryInfo(chain_id=receipt.receipt_id)
        return receipt

    def sign(self, receipt):
        return self.signer.sign(receipt)

    async def verify(self, contract: CompletionContract, tenant_id: str = "default",
                     assurance_level: str = "submitted", baselines=None):
        started = time.perf_counter()
        results = await self._conditions(contract.postconditions, contract, tenant_id)
        results = self.evaluate_transitions(contract, results, assurance_level, baselines)
        receipt = self.build_receipt(contract, results, assurance_level=assurance_level,
                                     duration_ms=(time.perf_counter() - started) * 1000)
        return self.sign(receipt)


    @staticmethod
    def _summary(results: list[ConditionResult]) -> VerificationSummary:
        return VerificationSummary(
            total=len(results),
            required=sum(1 for r in results if r.required),
            passed=sum(1 for r in results if r.status == ConditionStatus.PASS),
            failed=sum(1 for r in results if r.status == ConditionStatus.FAIL),
            unknown=sum(1 for r in results if r.status == ConditionStatus.UNKNOWN),
            providers=sorted({r.evidence.provider for r in results}),
        )

    @staticmethod
    def _verdict(results: list[ConditionResult]) -> Verdict:
        required = [r for r in results if r.required]
        if required and all(r.status == ConditionStatus.PASS for r in required):
            return Verdict.VERIFIED
        if any(r.status == ConditionStatus.UNKNOWN for r in required):
            return Verdict.UNKNOWN
        passes = sum(r.status == ConditionStatus.PASS for r in required)
        fails = sum(r.status == ConditionStatus.FAIL for r in required)
        if fails and not passes:
            return Verdict.FAILED
        if passes and fails:
            return Verdict.PARTIAL
        return Verdict.UNKNOWN
