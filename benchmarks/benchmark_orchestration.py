"""Comparable synthetic stage benchmarks; never sends requests to real providers."""
from __future__ import annotations

import argparse
import asyncio
import gc
import importlib.util
import json
import os
import platform
import statistics
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.benchmark_core import settings  # noqa: E402
from doneproof.adapters.base import ProviderAdapter, ProviderObservation  # noqa: E402
from doneproof.domain import CompletionContract, Postcondition  # noqa: E402
from doneproof.engine import VerificationEngine  # noqa: E402
from doneproof.job_models import JobContract  # noqa: E402
from doneproof.signing import ReceiptSigner  # noqa: E402
from doneproof.store import Store  # noqa: E402
from doneproof.worker import VerificationWorker  # noqa: E402


class SyntheticProvider(ProviderAdapter):
    def __init__(self, delay=0):
        self.delay = delay
        self.active = 0
        self.peak = 0

    async def observe(self, selector, context):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            return ProviderObservation({"ok": True}, source_url="benchmark://authoritative-stub")
        finally:
            self.active -= 1


def workload(size):
    conditions = [Postcondition.model_validate({
        "id": f"p{i}", "description": f"condition {i}", "provider": "unresolved",
        "selector": {"record": i}, "predicate": {"op": "eq", "path": "ok", "expected": True}
    }) for i in range(size)]
    # Large baseline cases deliberately measure the engine below its legacy API's 50-condition limit.
    base = CompletionContract(task="Synthetic orchestration workload", postconditions=conditions[:1])
    return base.model_copy(update={"postconditions": conditions})


def baseline_engine():
    spec = importlib.util.spec_from_file_location("doneproof.phase1_benchmark_engine", ROOT / "benchmarks/phase1_engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.VerificationEngine


@contextmanager
def durable_store(postgres):
    if not postgres:
        with TemporaryDirectory(prefix="doneproof-benchmark-") as directory:
            try:
                yield Store(str(Path(directory) / "jobs.db"))
            finally:
                # Legacy SQLite read helpers rely on connection finalizers. Release their cycles
                # before Windows attempts to remove the temporary database directory.
                gc.collect()
        return
    import psycopg
    from psycopg import sql
    dsn = os.environ["TEST_DATABASE_URL"]
    schema = "benchmark_" + uuid4().hex
    with psycopg.connect(dsn) as con:
        con.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query))
    query["options"] = "-csearch_path=" + schema
    try:
        yield Store(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)))
    finally:
        with psycopg.connect(dsn) as con:
            con.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


async def measure(mode, repeats, store=None):
    rows = []
    cfg = settings(":memory:")
    cls = baseline_engine() if mode == "baseline" else VerificationEngine
    for delay in (0, 0.005):
        for size in (1, 10, 100, 1000):
            provider = SyntheticProvider(delay)
            engine = cls({"unresolved": provider}, ReceiptSigner(cfg))
            contract = workload(size)
            worker = VerificationWorker(store, engine) if store else None
            async def execute():
                if worker:
                    job, _ = worker.db.create("benchmark", uuid4().hex, "synthetic", JobContract.model_validate(contract.model_dump()), {}, "submitted", 3600)
                    row = await worker.run_until_terminal("benchmark", job["id"])
                    assert row["state"] == "COMPLETE"
                    return store.get_receipt("benchmark", row["receipt_id"])
                return await engine.verify(contract)
            await execute()
            samples = []
            for _ in range(repeats):
                start = time.perf_counter()
                receipt = await execute()
                samples.append((time.perf_counter() - start) * 1000)
                assert receipt.verdict.value == "VERIFIED" and len(receipt.results) == size
            ordered = sorted(samples)
            rows.append({"conditions": size, "provider_delay_ms": delay * 1000,
                         "p50_ms": round(statistics.median(samples), 3),
                         "p95_ms": round(ordered[min(len(ordered)-1, int(len(ordered)*0.95))], 3),
                         "mean_ms": round(statistics.mean(samples), 3),
                         "peak_provider_concurrency": provider.peak})
            print(json.dumps(rows[-1]), flush=True)
    return {"mode": mode, "baseline_commit": "c33ad5f5918d36300d3581e700dffcb39c940ba9",
            "captured_at": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
            "platform": platform.platform(), "repeats": repeats, "results": rows,
            "storage": store.backend if store else None,
            "note": ("Synthetic durable end-to-end timings, including creation, leases, checkpoints, signing and receipt read. " if store else
                     "Synthetic engine-only timings, including evaluation and signing; no database. ") + "No real provider network. "
                    "100/1000-condition baseline workloads bypass the legacy API's 50-condition validation limit."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["baseline", "sync", "durable"], default="baseline")
    parser.add_argument("--postgres", action="store_true", help="Use an isolated schema in TEST_DATABASE_URL for durable timings")
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "durable":
        with durable_store(args.postgres) as store:
            result = asyncio.run(measure(args.mode, args.repeats, store))
    else:
        result = asyncio.run(measure(args.mode, args.repeats))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
