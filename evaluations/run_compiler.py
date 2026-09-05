"""Repeatable compiler evaluation with explicit provider worlds and optional live Astra."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import platform
import statistics
import sys
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from doneproof.adapters.base import ProviderAdapter, ProviderObservation  # noqa: E402
from doneproof.compilation import ContractCompiler  # noqa: E402
from doneproof.config import WebhookSource, get_settings  # noqa: E402
from doneproof.domain import ConditionStatus, Verdict  # noqa: E402
from doneproof.engine import VerificationEngine  # noqa: E402
from doneproof.selector_resolution import SelectorResolver  # noqa: E402
from doneproof.signing import ReceiptSigner  # noqa: E402
from evaluations.corpus import corpus  # noqa: E402

SOURCES = ["erp", "warehouse", "billing", "deploy", "support", "crm", "hr", "payments", "signing", "data",
           "analytics", "security", "backup", "catalog"]


class FixtureConnections:
    def __init__(self, status):
        self.status = status

    def capability(self, tenant, provider):
        return "disabled" if self.status == "disabled" else "available"

    async def usable(self, tenant, provider):
        return None


class FixtureProvider(ProviderAdapter):
    def __init__(self, provider, resources):
        self.provider, self.resources = provider, copy.deepcopy(resources)
        self.phase = "preflight"

    async def observe(self, selector, context):
        matches = []
        for resource in self.resources:
            if resource["provider"] != self.provider or resource["future"] and self.phase != "after":
                continue
            state = resource["after"] if self.phase == "after" else resource["before"]
            lookup = {**state, **resource["lookup"]}
            ok = True
            for key, value in selector.items():
                if key in {"created_after", "location"}:
                    continue
                actual = lookup.get(key)
                if (value not in actual if isinstance(actual, list) else actual != value):
                    ok = False
            if ok:
                matches.append(state)
        if len(matches) > 1:
            return ProviderObservation({"candidate_count": len(matches)}, indeterminate=True)
        return ProviderObservation(copy.deepcopy(matches[0]) if matches else None)


def signature(goal):
    goal = dict(goal)
    if goal["op"] == "contains_all" and isinstance(goal["expected"], list) and len(goal["expected"]) == 1:
        goal["op"], goal["expected"] = "contains", goal["expected"][0]
    return json.dumps(goal, sort_keys=True, separators=(",", ":"))


def returned_goals(contract):
    return {signature({"provider": p.provider, "selector": p.selector, "op": p.predicate.op,
        "path": p.predicate.path, "expected": p.predicate.expected, "require_change": p.require_change})
        for p in contract.postconditions if p.required}


async def evaluate_case(case, settings, mode):
    sources = {} if case["connection"] == "disabled" else {
        s: WebhookSource(tenant_id="evaluation", secret="fixture-only-signing-key") for s in SOURCES}
    configured = replace(settings, webhook_sources=sources, openai_api_key=settings.openai_api_key if mode == "live" else None)
    adapters = {p: FixtureProvider(p, case["resources"]) for p in ("github", "gmail", "webhook")}
    service = ContractCompiler(configured, SelectorResolver(adapters, FixtureConnections(case["connection"]), configured))
    result = await service.compile(case["task"], case["context"], "evaluation")
    false_certifiable, unnecessary_unknown, correct = False, False, False
    verification_executed, negative_worlds = False, 0
    if result.contract:
        golden = {signature(g) for g in case["expected_conditions"]}
        actual = returned_goals(result.contract)
        correct = case["expected_status"] == "valid_contract" and golden == actual
        false_certifiable = case["expected_status"] != "valid_contract" or not golden.issubset(actual)
        engine = VerificationEngine(adapters, ReceiptSigner(configured))
        for adapter in adapters.values():
            adapter.phase = "before"
        baselines = {p.id: p for p in await engine.snapshot(result.contract, "evaluation")}
        for adapter in adapters.values():
            adapter.phase = "after"
        receipt = await engine.verify(result.contract, "evaluation", "registered", baselines)
        verification_executed = True
        unnecessary_unknown = (case["expected_status"] == "valid_contract" and
                               any(r.status == ConditionStatus.UNKNOWN for r in receipt.results))
        # For each golden requirement, independently violate that one outcome.
        for goal in case["expected_conditions"]:
            adapter = adapters[goal["provider"]]
            saved = copy.deepcopy(adapter.resources)
            for resource in adapter.resources:
                state = resource["after"]
                parts = goal["path"].split(".")
                for part in parts[:-1]:
                    state = state.setdefault(part, {})
                value = state.get(parts[-1])
                state[parts[-1]] = (not value if type(value) is bool else [] if isinstance(value, list)
                                       else -1 if type(value) in (int, float) else "counterexample-value")
            negative = await engine.verify(result.contract, "evaluation", "registered", baselines)
            false_certifiable |= negative.verdict == Verdict.VERIFIED
            negative_worlds += 1
            adapter.resources = saved
    return {"id": case["id"], "provider": case["provider"], "expected_status": case["expected_status"],
        "status": result.status, "correct_contract": correct, "false_certifiable": false_certifiable,
        "unnecessary_unknown": unnecessary_unknown, "verification_executed": verification_executed,
        "negative_worlds_checked": negative_worlds, "deterministic": result.deterministic,
        "selector_checks": [c.status for c in result.selector_checks],
        "clarification_codes": sorted({p.code for p in result.clarification_requirements}),
        "latency_ms": result.latency_ms, "usage": result.usage.model_dump()}


def ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "rate": round(numerator / denominator, 6) if denominator else None}


def summarize(rows, mode):
    checks = Counter(s for r in rows for s in r["selector_checks"])
    accepted = sum(r["status"] == "valid_contract" for r in rows)
    tokens = {k: sum(r["usage"][k] for r in rows) for k in
              ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens")}
    complete = all(r["usage"]["complete"] for r in rows)
    cost = ((max(0, tokens["input_tokens"] - tokens["cached_input_tokens"] - tokens["cache_write_tokens"]) * 10
             + tokens["cached_input_tokens"] + tokens["cache_write_tokens"] * 12.5 + tokens["output_tokens"] * 50) / 1_000_000)
    latency = sorted(r["latency_ms"] for r in rows)
    return {
        "mode": mode, "provider_environment": "deterministic authoritative adapter fixtures; no live provider traffic",
        "model_environment": "live Astra Responses API" if mode == "live" else "model disabled; unsupported language returns clarification",
        "tasks": len(rows), "provider_counts": dict(Counter(r["provider"] for r in rows)),
        "valid_contract_rate": ratio(accepted, len(rows)),
        "valid_contract_recall": ratio(sum(r["correct_contract"] for r in rows), sum(r["expected_status"] == "valid_contract" for r in rows)),
        "selector_resolution_rate": ratio(checks["resolved"], sum(v for k, v in checks.items() if k != "deferred")),
        "selector_executable_rate": ratio(checks["resolved"] + checks["deferred"], sum(checks.values())),
        "selector_checks": dict(checks),
        "false_certifiable_contract_rate": ratio(sum(r["false_certifiable"] for r in rows), accepted),
        "unnecessary_unknown_rate": ratio(sum(r["unnecessary_unknown"] for r in rows), sum(r["verification_executed"] for r in rows)),
        "clarification_rate": ratio(len(rows) - accepted, len(rows)),
        "status_accuracy": ratio(sum(r["status"] == r["expected_status"] for r in rows), len(rows)),
        "negative_worlds_checked": sum(r["negative_worlds_checked"] for r in rows),
        "compilation_latency_ms": {"p50": statistics.median(latency), "p95": latency[math.ceil(len(latency) * .95) - 1], "max": max(latency)},
        "token_usage": {**tokens, "complete": complete, "model_calls": sum(len(r["usage"]["efforts"]) for r in rows)},
        "estimated_token_cost_usd": round(cost, 8) if complete else None,
        "pricing": {"as_of": "2026-09-05", "model": "gpt-6-astra", "per_million":
                    {"input": 10, "cached_input": 1, "cache_write": 12.5, "output": 50},
                    "source": "https://developers.openai.com/api/docs/models/gpt-6-astra",
                    "note": "Standard short-context list-price estimate; not an invoice. No API calls means zero token cost."},
    }


async def evaluate(mode="offline", limit=None):
    settings = get_settings()
    if mode == "live" and not settings.openai_api_key:
        raise RuntimeError("Live evaluation requires OPENAI_API_KEY in the process environment.")
    cases = corpus()[:limit]
    rows = [await evaluate_case(case, settings, mode) for case in cases]
    return {"evaluated_at": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
            "summary": summarize(rows, mode), "cases": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--export-corpus", type=Path)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    result = asyncio.run(evaluate(args.mode, args.limit))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.export_corpus:
        args.export_corpus.parent.mkdir(parents=True, exist_ok=True)
        args.export_corpus.write_text("".join(json.dumps(c) + "\n" for c in corpus()), encoding="utf-8")
    print(json.dumps(result["summary"], indent=2))
    return int(result["summary"]["false_certifiable_contract_rate"]["numerator"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
