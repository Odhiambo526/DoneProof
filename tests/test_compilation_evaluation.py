from __future__ import annotations

import asyncio
from collections import Counter

from evaluations.corpus import corpus
from evaluations.run_compiler import evaluate


def test_corpus_has_independent_outcomes_and_balanced_provider_coverage():
    cases = corpus()
    assert len(cases) >= 100
    assert len({c["id"] for c in cases}) == len(cases)
    assert min(Counter(c["provider"] for c in cases).values()) >= 30
    assert {c["expected_status"] for c in cases} == {
        "valid_contract", "unsupported_provider", "missing_identifier", "ambiguous_resource", "unverifiable_outcome"}
    assert all(c["expected_conditions"] and c["resources"] for c in cases if c["expected_status"] == "valid_contract")
    assert any(c["context"] for c in cases)


def test_offline_corpus_correctness_gate_and_honest_measurement():
    report = asyncio.run(evaluate())
    summary = report["summary"]
    assert summary["false_certifiable_contract_rate"]["numerator"] == 0
    assert summary["unnecessary_unknown_rate"]["numerator"] == 0
    assert summary["valid_contract_recall"]["rate"] >= 0.9
    assert summary["negative_worlds_checked"] >= 100
    assert summary["token_usage"]["model_calls"] == 0
    assert summary["estimated_token_cost_usd"] == 0
    assert summary["selector_checks"]["deferred"] > 0
    assert summary["selector_checks"]["ambiguous"] > 0
    assert "model disabled" in summary["model_environment"]
