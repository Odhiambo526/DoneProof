"""Pure explanations, never instructions to a provider or inputs to predicate evaluation."""
import json

from .recovery_models import Remediation
from .security import _SENSITIVE_KEY, sanitize

GUIDANCE_FIELDS = frozenset({"remediation", "action_hint", "reverify_after", "recovery", "previous_receipt_id"})


def contains_guidance(value):
    if isinstance(value, dict):
        if value.get("kind") == "doneproof.remediation":
            return True
        if {"condition", "status", "action_hint", "reverify_after"} <= value.keys():
            return True
        if {"receipt_id", "results", "signature"} <= value.keys():
            return True
        return any(contains_guidance(item) for item in value.values())
    return any(contains_guidance(item) for item in value) if isinstance(value, list) else False


def display_value(value, path):
    if any(_SENSITIVE_KEY.search(part) for part in path.split(".")):
        return "[REDACTED]"
    value = sanitize(value)
    # Avoid copying large provider objects into guidance. Full retained evidence stays in results.
    return value if len(json.dumps(value, ensure_ascii=False)) <= 2048 else "[See condition evidence]"


def remediation_for(results):
    entries = []
    for result in results:
        if result.status == "PASS":
            continue
        code, hint, after, retryable = (
            "predicate_unsatisfied", "Authoritative state does not satisfy this condition. An external actor may repair it.",
            "external_action", True)
        if result.status == "UNKNOWN":
            code, hint, after = ("evidence_unavailable",
                "Authoritative evidence is incomplete or unavailable. Establish provider access or wait for fresh evidence.",
                "authoritative_evidence")
        if result.evidence.provider == "unresolved" or any(
                part in GUIDANCE_FIELDS or _SENSITIVE_KEY.search(part) for part in result.predicate.path.split(".")):
            code, hint, after, retryable = ("contract_not_verifiable",
                "This condition needs a verifiable contract before it can be certified.", "contract_revision", False)
        elif result.transition_required and result.baseline_status != "FAIL":
            code, hint, after, retryable = ("transition_not_provable",
                "The original baseline cannot prove the requested transition. Register a new run before a new external action.",
                "new_registered_run", False)
        elif (result.evidence.provider == "gmail" and result.status == "FAIL"
              and result.predicate.path == "location" and result.predicate.op == "eq"
              and str(result.predicate.expected).upper() == "SENT"
              and str(result.evidence.observed).upper() == "DRAFT"):
            code, hint = "message_is_draft", "The message exists but is not in SENT state."
        entries.append(Remediation(condition=result.id, status=result.status,
            expected=display_value(result.predicate.expected, result.predicate.path),
            observed=display_value(result.evidence.observed, result.predicate.path), retryable=retryable,
            code=code, action_hint=hint, reverify_after=after))
    return entries


def failure_patterns(receipts):
    """Report A/B/A/B status cycles and three consecutive identical non-PASS outcomes."""
    oscillating, repeated = [], []
    if not receipts:
        return oscillating, repeated
    for result in receipts[-1].results:
        history = [next((r for r in receipt.results if r.id == result.id), None) for receipt in receipts]
        states = [r.status if r else None for r in history]
        fingerprints = [(r.status, json.dumps(r.evidence.observed, sort_keys=True)) if r else None for r in history]
        if (len(fingerprints) >= 4 and fingerprints[-4] == fingerprints[-2]
                and fingerprints[-3] == fingerprints[-1] and fingerprints[-1] != fingerprints[-2]):
            oscillating.append(result.id)
        if len(states) >= 3 and states[-1] != "PASS" and len(set(states[-3:])) == 1:
            observed = [json.dumps(r.evidence.observed, sort_keys=True) for r in history[-3:] if r]
            if len(observed) == 3 and len(set(observed)) == 1:
                repeated.append(result.id)
    return oscillating, repeated
