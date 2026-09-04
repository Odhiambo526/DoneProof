from __future__ import annotations

import asyncio
import base64
import json
import statistics
import tempfile
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from doneproof.adapters.mock import MockAdapter
from doneproof.config import Settings
from doneproof.domain import CompletionContract
from doneproof.engine import VerificationEngine
from doneproof.signing import ReceiptSigner
from doneproof.store import Store


def settings(db: str) -> Settings:
    return Settings(
        env="benchmark",
        db_path=db,
        api_keys={},
        cors_origins=(),
        enable_demo=True,
        verification_timeout_seconds=3.0,
        openai_api_key=None,
        openai_model="gpt-6-astra",
        github_token=None,
        gmail_tokens={},
        gmail_access_token=None,
        webhook_sources={},
        webhook_max_skew_seconds=600,
        signing_seed_b64=base64.b64encode(b"B" * 32).decode(),
        legacy_receipt_key=None,
        max_body_bytes=1048576,
    )


def contract(n: int = 8) -> CompletionContract:
    return CompletionContract.model_validate(
        {
            "task": "Benchmark deterministic verification",
            "postconditions": [
                {
                    "id": f"p{i}",
                    "description": f"condition {i}",
                    "provider": "mock",
                    "selector": {"state": {"ok": True, "index": i}},
                    "predicate": {"op": "eq", "path": "ok", "expected": True},
                    "required": True,
                }
                for i in range(n)
            ],
        }
    )


async def run_engine(engine: VerificationEngine, c: CompletionContract, count: int) -> tuple[list[float], list]:
    latencies: list[float] = []
    receipts = []
    for _ in range(count):
        start = time.perf_counter()
        receipts.append(await engine.verify(c, assurance_level="registered"))
        latencies.append((time.perf_counter() - start) * 1000)
    return latencies, receipts


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return ordered[idx]


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg = settings(str(Path(td) / "bench.db"))
        engine = VerificationEngine({"mock": MockAdapter()}, ReceiptSigner(cfg), timeout_seconds=2)
        c = contract()
        count = 1000
        t0 = time.perf_counter()
        latencies, receipts = asyncio.run(run_engine(engine, c, count))
        engine_seconds = time.perf_counter() - t0

        store = Store(cfg.db_path)
        store.save_contract("benchmark", c)
        t1 = time.perf_counter()
        for receipt in receipts:
            store.save_receipt("benchmark", receipt)
        store_seconds = time.perf_counter() - t1

        report = {
            "runs": count,
            "conditions_per_run": len(c.postconditions),
            "engine": {
                "throughput_receipts_per_second": round(count / engine_seconds, 1),
                "mean_ms": round(statistics.mean(latencies), 3),
                "p50_ms": round(percentile(latencies, 0.50), 3),
                "p95_ms": round(percentile(latencies, 0.95), 3),
                "p99_ms": round(percentile(latencies, 0.99), 3),
            },
            "sqlite_persistence": {
                "writes_per_second": round(count / store_seconds, 1),
                "total_seconds": round(store_seconds, 3),
            },
            "note": "Synthetic local benchmark. Real verification latency is normally dominated by provider network/API latency.",
        }
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
