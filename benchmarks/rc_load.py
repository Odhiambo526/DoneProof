"""Bounded synthetic queue saturation. No live provider or staging-soak claim."""
import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from doneproof.adapters.base import ProviderAdapter, ProviderObservation  # noqa: E402
from doneproof.config import get_settings  # noqa: E402
from doneproof.domain import CompletionContract  # noqa: E402
from doneproof.engine import VerificationEngine  # noqa: E402
from doneproof.job_store import JobStore  # noqa: E402
from doneproof.signing import ReceiptSigner  # noqa: E402
from doneproof.store import Store  # noqa: E402
from doneproof.worker import VerificationWorker  # noqa: E402


class ControlledProvider(ProviderAdapter):
    def __init__(self):
        self.active = self.peak = 0

    async def observe(self, selector, context):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(.005)
            return ProviderObservation({"state": "closed"})
        finally:
            self.active -= 1


async def load(path, count, workers):
    store = Store(str(path))
    db = JobStore(store)
    provider = ControlledProvider()
    settings = replace(get_settings(), openai_api_key=None, signing_seed_b64=None,
                       legacy_receipt_key="offline-load-fixture-only")
    engine = VerificationEngine({"github": provider}, ReceiptSigner(settings))
    contract = CompletionContract(task="Controlled synthetic load fixture", postconditions=[{
        "id": "p1", "provider": "github", "description": "Fixture status", "selector": {"repo": "fixture/load", "kind": "issue", "number": 1},
        "predicate": {"op": "eq", "path": "state", "expected": "closed"}}])
    started = time.monotonic()
    for i in range(count):
        db.create("load-fixture", f"load-{i}", f"hash-{i}", contract, {}, "submitted", 3600)
    async def consume():
        worker = VerificationWorker(store, engine)
        while await worker.tick():
            pass
    await asyncio.wait_for(asyncio.gather(*(consume() for _ in range(workers))), 900)
    with db.transaction() as con:
        jobs = [dict(r) for r in db.execute(con, "SELECT * FROM verification_jobs")]
        receipts = db.execute(con, "SELECT COUNT(*) AS n FROM receipts").fetchone()["n"]
        stale = db.execute(con, "SELECT COUNT(*) AS n FROM verification_provider_slots WHERE lease_token IS NOT NULL").fetchone()["n"]
    if receipts != count or stale or any(j["state"] != "COMPLETE" for j in jobs):
        raise RuntimeError("Synthetic saturation gate failed")
    latency = sorted(j["finished_at"] - j["created_at"] for j in jobs)
    return {"fixture": True, "storage": "sqlite", "queued_jobs": count, "workers": workers,
            "completed_receipts": receipts, "stale_provider_slots": stale,
            "provider_peak_concurrency": provider.peak, "elapsed_seconds": time.monotonic() - started,
            "queue_wait_mean_seconds": statistics.mean(j["started_at"] - j["created_at"] for j in jobs),
            "verification_seconds": {f"p{p}": latency[min(len(latency)-1, int(len(latency)*p/100))] for p in (50,95,99)},
            "limitation": "Synthetic local saturation only; no live providers, callbacks, browser load or multi-hour soak"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 1000 or not 1 <= args.workers <= 16:
        parser.error("Jobs must be 1..1000; workers 1..16")
    with tempfile.TemporaryDirectory() as temporary:
        result = asyncio.run(load(Path(temporary) / "load.db", args.jobs, args.workers))
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
