from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os

from .adapters.base import ProviderAdapter
from .domain import (
    CompletionContract,
    ConditionResult,
    ConditionStatus,
    Evidence,
    VerificationReceipt,
    Verdict,
)
from .predicates import evaluate


class VerificationEngine:
    def __init__(self, adapters: dict[str, ProviderAdapter], receipt_key: str | None = None):
        self.adapters = adapters
        self.receipt_key = (receipt_key or os.getenv("DONEPROOF_RECEIPT_KEY", "dev-only-change-me")).encode()

    async def _verify_one(self, pc, contract: CompletionContract):
        adapter = self.adapters.get(pc.provider)
        selector = dict(pc.selector)
        # Discovery selectors deliberately do not need to repeat task timing.
        # The verifier injects the trusted contract boundary instead.
        if pc.provider == "github" and selector.get("number") is None and "created_after" not in selector:
            selector["created_after"] = contract.task_started_at.isoformat()

        if adapter is None:
            return ConditionResult(
                id=pc.id,
                description=pc.description,
                required=pc.required,
                status=ConditionStatus.UNKNOWN,
                predicate=pc.predicate,
                evidence=Evidence(provider=pc.provider, selector=selector, note="No adapter registered"),
                reason=f"provider '{pc.provider}' is unavailable",
            )
        try:
            observation = await adapter.observe(selector)
            if observation.indeterminate:
                return ConditionResult(
                    id=pc.id,
                    description=pc.description,
                    required=pc.required,
                    status=ConditionStatus.UNKNOWN,
                    predicate=pc.predicate,
                    evidence=Evidence(
                        provider=pc.provider,
                        selector=selector,
                        observed=observation.state,
                        source_url=observation.source_url,
                        note=observation.note,
                    ),
                    reason=observation.note or "provider could not establish a unique authoritative state",
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
                    selector=selector,
                    observed=observed,
                    source_url=observation.source_url,
                    note=observation.note,
                ),
                reason=reason,
            )
        except Exception as exc:
            return ConditionResult(
                id=pc.id,
                description=pc.description,
                required=pc.required,
                status=ConditionStatus.UNKNOWN,
                predicate=pc.predicate,
                evidence=Evidence(provider=pc.provider, selector=selector, note=str(exc)),
                reason=f"verification error: {type(exc).__name__}: {exc}",
            )

    async def verify(self, contract: CompletionContract) -> VerificationReceipt:
        results = await asyncio.gather(*(self._verify_one(pc, contract) for pc in contract.postconditions))
        verdict = self._verdict(results)
        receipt = VerificationReceipt(
            contract_id=contract.id,
            task=contract.task,
            verdict=verdict,
            results=results,
        )
        canonical = receipt.model_dump(mode="json", exclude={"receipt_hash", "signature"})
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        receipt.receipt_hash = hashlib.sha256(payload).hexdigest()
        receipt.signature = hmac.new(self.receipt_key, payload, hashlib.sha256).hexdigest()
        return receipt

    @staticmethod
    def _verdict(results: list[ConditionResult]) -> Verdict:
        required = [r for r in results if r.required]
        optional = [r for r in results if not r.required]
        if required and all(r.status == ConditionStatus.PASS for r in required):
            if any(r.status in {ConditionStatus.FAIL, ConditionStatus.UNKNOWN} for r in optional):
                return Verdict.PARTIAL
            return Verdict.VERIFIED
        if any(r.status == ConditionStatus.FAIL for r in required):
            if any(r.status == ConditionStatus.PASS for r in results):
                return Verdict.PARTIAL
            return Verdict.FAILED
        return Verdict.UNKNOWN
