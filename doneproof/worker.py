"""Run with python -m doneproof.worker on persistent compute, alongside the API."""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone

from .job_callbacks import CallbackRegistry
from .job_models import TERMINAL
from .job_store import JobStore, evaluation_inputs
from .pipeline import ObservationRecord
from .recovery_store import RecoveryStore
from .retries import TransientObservationError

logger = logging.getLogger("doneproof.worker")


class VerificationWorker:
    def __init__(self, store, engine, callbacks=None, *, batch_size=16, callback_transport=None, recovery=None):
        self.db = JobStore(store)
        if any(engine.registry.require(d.manifest.provider_id).fingerprint != d.fingerprint for d in store.registry):
            raise ValueError("Worker and storage provider registries must match")
        self.engine = engine
        self.recovery = recovery or RecoveryStore(store)
        self.callbacks = callbacks or CallbackRegistry({})
        self.batch_size = max(1, min(batch_size, 64))
        # A whole batch is concurrent and bounded by one observation timeout. The grace covers DB/CPU work.
        self.lease_seconds = max(90, engine.timeout_seconds + 60)
        self.callback_transport = callback_transport

    async def _one(self, job, contract, pc, claim):
        try:
            observation = await self.engine.observe(pc, contract, job["tenant_id"], durable=True)
            return claim, observation.checkpoint(), None, 0
        except TransientObservationError as exc:
            return claim, None, exc, self.db.registry.policy(pc.provider).delay(claim["attempts"], exc.retry_after)

    async def _observe(self, job):
        claims = self.db.claim_conditions(job, self.batch_size, self.lease_seconds)
        contract, _, _ = evaluation_inputs(job)
        pcs = {pc.id: pc for pc in contract.postconditions}
        tasks = [asyncio.create_task(self._one(job, contract, pcs[c["condition_id"]], c)) for c in claims]
        try:
            pending = set(tasks)
            while pending:
                _, pending = await asyncio.wait(pending, timeout=0.25)
                if pending and not self.db.active(job):
                    return
            self.db.finish_observations(job, [task.result() for task in tasks])
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.db.release_slots(claims)

    def _evaluate(self, job):
        contract, baselines, created = evaluation_inputs(job)
        rows = self.db.conditions(job["tenant_id"], job["id"])
        results = [self.engine.evaluate_observation(pc, contract, ObservationRecord.model_validate_json(row["observation_json"]))
                   for pc, row in zip(contract.postconditions, rows, strict=True)]
        results = self.engine.evaluate_transitions(contract, results, job["assurance_level"], baselines)
        with self.db.transaction() as con:
            now = datetime.fromtimestamp(self.db.now(con), timezone.utc)
        receipt = self.engine.build_receipt(contract, results, assurance_level=job["assurance_level"],
            receipt_id=job["receipt_id"], verified_at=now, duration_ms=max(0, (now - created).total_seconds() * 1000))
        self.db.save_evaluation(job, receipt, self.engine.signer.key_id)

    async def tick(self):
        job = self.db.claim(self.lease_seconds)
        if not job:
            return False
        try:
            if job["state"] == "OBSERVING":
                await self._observe(job)
            elif job["state"] == "EVALUATING":
                self._evaluate(job)
            elif job["state"] == "SIGNING":
                self.db.publish(job, self.engine)
        except asyncio.CancelledError:
            self.db.abandon(job)
            raise
        except Exception as exc:
            # Provider error text can contain credentials. Log a fixed event and exception class only.
            logger.error("verification_job_error error_type=%s", type(exc).__name__)
            self.db.fail_job(job)
        return True

    async def recovery_tick(self):
        return await asyncio.to_thread(self.recovery.dispatch_event)

    async def callback_tick(self):
        row = self.db.claim_callback()
        if not row:
            return False
        await self.callbacks.deliver(self.db, row, self.callback_transport)
        return True

    async def run_until_terminal(self, tenant, job_id):
        """Useful for integration tests and benchmarks; HTTP requests never invoke this loop."""
        while True:
            row = self.db.get_job(tenant, job_id)
            if row["state"] in TERMINAL:
                return row
            if not await self.tick():
                await asyncio.sleep(0.05)

    async def _loop(self, operation):
        while True:
            try:
                if not await operation():
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("verification_worker_unavailable error_type=%s", type(exc).__name__)
                await asyncio.sleep(1)

    async def run(self):
        # A slow callback must not consume the next verification's deadline.
        async with asyncio.TaskGroup() as group:
            group.create_task(self._loop(self.tick))
            group.create_task(self._loop(self.callback_tick))
            group.create_task(self._loop(self.recovery_tick))


async def serve():
    from .app import app
    worker = VerificationWorker(app.state.store, app.state.engine, app.state.job_callbacks, recovery=app.state.recovery)
    task = asyncio.create_task(worker.run())
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, task.cancel)
        except NotImplementedError:  # Windows developer environments
            signal.signal(name, lambda *_: loop.call_soon_threadsafe(task.cancel))
    try:
        await task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(serve())
