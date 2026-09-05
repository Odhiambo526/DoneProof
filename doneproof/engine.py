from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
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
from .predicates import evaluate
from .security import sanitize
from .signing import ReceiptSigner

logger = logging.getLogger("doneproof.verification")


class VerificationEngine:
    def __init__(
        self,
        adapters: dict[str, ProviderAdapter],
        signer: ReceiptSigner,
        timeout_seconds: float = 15.0,
    ):
        self.adapters = adapters
        self.signer = signer
        self.timeout_seconds = timeout_seconds

    async def _verify_one(self, pc, contract: CompletionContract, tenant_id: str, capture=False) -> ConditionResult:
        started = time.perf_counter()
        adapter = self.adapters.get(pc.provider)
        selector = dict(pc.selector)
        if pc.provider in {"github", "gmail", "webhook"} and self._is_discovery(pc.provider, selector):
            # The contract's trusted run boundary always wins. Never accept a
            # model/caller supplied earlier timestamp for discovery.
            selector["created_after"] = contract.task_started_at.isoformat()
        safe_selector = sanitize(selector)
        context = ObservationContext(
            tenant_id=tenant_id,
            contract_id=contract.id,
            task_started_at=contract.task_started_at.isoformat(),
            condition_id=pc.id,
            require_connection_binding=pc.require_change,
            capture_connection_binding=capture,
        )
        if adapter is None:
            return self._result_unknown(pc, safe_selector, "Provider is not available in this deployment.", started)
        try:
            observation = await asyncio.wait_for(adapter.observe(selector, context), timeout=self.timeout_seconds)
            latency = (time.perf_counter() - started) * 1000
            if observation.indeterminate:
                return ConditionResult(
                    id=pc.id,
                    description=pc.description,
                    required=pc.required,
                    status=ConditionStatus.UNKNOWN,
                    predicate=pc.predicate,
                    evidence=Evidence(
                        provider=pc.provider,
                        selector=safe_selector,
                        observed=sanitize(observation.state),
                        source_url=observation.source_url,
                        note=observation.note,
                    ),
                    reason=observation.note or "Authoritative state could not be established.",
                    transition_required=pc.require_change,
                    latency_ms=round(latency, 2),
                )
            status, reason, observed = evaluate(observation.state, pc.predicate)
            return ConditionResult(
                id=pc.id,
                description=pc.description,
                required=pc.required,
                status=status,
                predicate=pc.predicate,
                evidence=Evidence(
                    provider=pc.provider,
                    selector=safe_selector,
                    observed=sanitize(observed),
                    source_url=observation.source_url,
                    note=observation.note,
                ),
                reason=reason,
                transition_required=pc.require_change,
                latency_ms=round(latency, 2),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "verification_timeout provider=%s contract_id=%s condition_id=%s",
                pc.provider,
                contract.id,
                pc.id,
            )
            return self._result_unknown(
                pc, safe_selector, "Verification timed out before authoritative state was established.", started
            )
        except Exception as exc:
            logger.warning(
                "verification_provider_error provider=%s contract_id=%s condition_id=%s error_type=%s",
                pc.provider,
                contract.id,
                pc.id,
                type(exc).__name__,
            )
            return self._result_unknown(
                pc, safe_selector, "Provider verification was unavailable or returned an invalid response.", started
            )

    def _result_unknown(self, pc, selector: dict[str, Any], reason: str, started: float) -> ConditionResult:
        return ConditionResult(
            id=pc.id,
            description=pc.description,
            required=pc.required,
            status=ConditionStatus.UNKNOWN,
            predicate=pc.predicate,
            evidence=Evidence(provider=pc.provider, selector=selector, note=reason),
            reason=reason,
            transition_required=pc.require_change,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    @staticmethod
    def _is_discovery(provider: str, selector: dict[str, Any]) -> bool:
        if provider == "github":
            return selector.get("number") is None
        if provider == "gmail":
            return selector.get("message_id") is None
        if provider == "webhook":
            return True
        return False

    async def snapshot(self, contract: CompletionContract, tenant_id: str = "default") -> list[ConditionResult]:
        targets = [pc for pc in contract.postconditions if pc.require_change]
        if not targets:
            return []
        return list(await asyncio.gather(*(self._verify_one(pc, contract, tenant_id, capture=True) for pc in targets)))

    async def verify(
        self,
        contract: CompletionContract,
        tenant_id: str = "default",
        assurance_level: str = "submitted",
        baselines: dict[str, ConditionResult] | None = None,
    ) -> VerificationReceipt:
        started = time.perf_counter()
        results = list(
            await asyncio.gather(*(self._verify_one(pc, contract, tenant_id) for pc in contract.postconditions))
        )
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
        contract_payload = json.dumps(
            contract.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        receipt = VerificationReceipt(
            assurance_level=assurance_level,
            contract_id=contract.id,
            contract_hash=hashlib.sha256(contract_payload).hexdigest(),
            task=contract.task,
            verdict=self._verdict(results),
            summary=self._summary(results),
            results=results,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return self.signer.sign(receipt)

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
