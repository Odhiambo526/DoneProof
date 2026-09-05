"""Durable queue transactions. Network I/O never runs inside these transactions."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from .connection_store import ConnectionStore
from .domain import ConditionResult, VerificationReceipt
from .job_models import TERMINAL, JobContract
from .pipeline import ObservationRecord


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class IdempotencyConflict(Exception):
    pass


class QueueFull(Exception):
    pass


class JobStore(ConnectionStore):
    def __init__(self, store):
        super().__init__(store)
        self.registry = store.registry

    def provider_manifest(self, contract):
        return {pc.provider: self.registry.require(pc.provider).fingerprint
                for pc in contract.postconditions if pc.provider != "unresolved"}

    def bind_contract(self, tenant, contract):
        with self.transaction() as con:
            for provider, fingerprint in self.provider_manifest(contract).items():
                self.execute(con, """INSERT INTO provider_contract_bindings(tenant_id,contract_id,provider,fingerprint)
                    VALUES(?,?,?,?) ON CONFLICT(tenant_id,contract_id,provider) DO NOTHING""",
                    (tenant, contract.id, provider, fingerprint))

    def binding_current(self, tenant, contract_id, provider):
        with self.transaction() as con:
            row = self._row(self.execute(con, """SELECT fingerprint FROM provider_contract_bindings
                WHERE tenant_id=? AND contract_id=? AND provider=?""", (tenant, contract_id, provider)))
        definition = self.registry.get(provider)
        return not row or bool(definition and row["fingerprint"] == definition.fingerprint)

    def providers_current(self, job):
        from .adapters.builtin_provider import builtin_definitions
        expected = json.loads(job["provider_manifest_json"])
        contract = JobContract.model_validate_json(job["contract_json"])
        names = {pc.provider for pc in contract.postconditions if pc.provider != "unresolved"}
        # Migration compatibility: pre-SDK jobs can only contain shipped provider
        # names. Compare them against the immutable shipped v1 declarations.
        if not expected:
            historical = {d.manifest.provider_id: d.fingerprint for d in builtin_definitions()}
            expected = {pc.provider: historical.get(pc.provider) for pc in contract.postconditions
                        if pc.provider != "unresolved"}
        return set(expected) == names and all(value and (definition := self.registry.get(name)) and definition.fingerprint == value
                   for name, value in expected.items())

    def execute_many(self, con, statement, values):
        cur = con.cursor()
        try:
            cur.executemany(statement.replace("?", "%s") if self.pg else statement, values)
        finally:
            cur.close()

    def now(self, con):
        return float(con.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS now").fetchone()["now"]) if self.pg else time.time()

    def lock(self, *, skip=False):
        return (" FOR UPDATE SKIP LOCKED" if skip else " FOR UPDATE") if self.pg else ""

    def row(self, con, tenant, job_id, *, lock=False):
        return self._row(self.execute(con, "SELECT * FROM verification_jobs WHERE tenant_id=? AND id=?" +
                                     (self.lock() if lock else ""), (tenant, job_id)))

    def create(self, tenant, key, request_hash, contract, baselines, assurance, deadline, callback=None):
        with self.transaction() as con:
            return self.create_in_transaction(con, tenant, key, request_hash, contract, baselines, assurance, deadline, callback)

    def create_in_transaction(self, con, tenant, key, request_hash, contract, baselines, assurance, deadline, callback=None):
        manifest = canonical(self.provider_manifest(contract))
        self.execute(con, "INSERT INTO verification_tenants(tenant_id) VALUES(?) ON CONFLICT DO NOTHING", (tenant,))
        self.execute(con, "SELECT tenant_id FROM verification_tenants WHERE tenant_id=?" + self.lock(), (tenant,))
        row = self._row(self.execute(con,
            "SELECT * FROM verification_jobs WHERE tenant_id=? AND idempotency_hash=?", (tenant, digest(key))))
        if row:
            if row["request_hash"] != request_hash:
                raise IdempotencyConflict
            return row, False
        count = self.execute(con, """SELECT COUNT(*) AS n FROM verification_jobs
            WHERE tenant_id=? AND finished_at IS NULL AND deadline_at>?""", (tenant, self.now(con))).fetchone()["n"]
        if count >= 1000:
            raise QueueFull
        now, identifier = self.now(con), uuid4().hex
        job_id = "vj_" + identifier
        self.execute(con, """INSERT INTO verification_jobs
            (tenant_id,id,idempotency_hash,request_hash,state,contract_json,baselines_json,assurance_level,
             condition_count,created_at,deadline_at,next_run_at,receipt_id,callback_id,callback_fingerprint,provider_manifest_json)
            VALUES(?,?,?,?,'QUEUED',?,?,?,?,?,?,?,?,?,?,?)""",
            (tenant, job_id, digest(key), request_hash, contract.model_dump_json(),
             canonical({k: v.model_dump(mode="json") for k, v in baselines.items()}), assurance,
             len(contract.postconditions), now, now + deadline, now, "vr_" + identifier,
             callback[0] if callback else None, callback[1] if callback else None, manifest))
        self.execute_many(con, """INSERT INTO verification_conditions
            (tenant_id,job_id,condition_id,ordinal,provider,state,next_attempt_at) VALUES(?,?,?,?,?,'PENDING',?)""",
            [(tenant, job_id, pc.id, ordinal, pc.provider, now) for ordinal, pc in enumerate(contract.postconditions)])
        return self.row(con, tenant, job_id), True

    def idempotent_job(self, tenant, key, request_hash):
        with self.transaction() as con:
            row = self._row(self.execute(con,
                "SELECT * FROM verification_jobs WHERE tenant_id=? AND idempotency_hash=?", (tenant, digest(key))))
            if row and row["request_hash"] != request_hash:
                raise IdempotencyConflict
            return row

    def registered(self, tenant, contract_id):
        with self.transaction() as con:
            return bool(self.execute(con, """SELECT 1 FROM audit_events WHERE tenant_id=? AND object_id=?
                AND object_type='contract' AND action='run.registered' LIMIT 1""", (tenant, contract_id)).fetchone())

    def _terminal(self, con, job, state, reason, now):
        if job["state"] in TERMINAL:
            return job
        self.execute(con, """UPDATE verification_jobs SET state=?,terminal_reason=?,finished_at=?,
            lease_token=NULL,lease_until=0,revision=revision+1 WHERE tenant_id=? AND id=?""",
            (state, reason, now, job["tenant_id"], job["id"]))
        if state in {"EXPIRED", "INTERNAL_ERROR"}:
            self.execute(con, """UPDATE verification_conditions SET state='ABORTED',lease_token=NULL,error_code=?
                WHERE tenant_id=? AND job_id=? AND state IN ('PENDING','RUNNING')""", (reason, job["tenant_id"], job["id"]))
            self.execute(con, """UPDATE verification_attempts SET finished_at=?,outcome='aborted',error_code=?
                WHERE tenant_id=? AND job_id=? AND finished_at IS NULL""", (now, reason, job["tenant_id"], job["id"]))
        updated = self.row(con, job["tenant_id"], job["id"])
        self.execute(con, """INSERT INTO audit_events(tenant_id,action,object_type,object_id,metadata_json,created_at)
            VALUES(?,'verification.job_finished','verification_job',?,?,?)""", (job["tenant_id"], job["id"],
                canonical({"state": state, "reason": reason}), datetime.fromtimestamp(now, timezone.utc).isoformat()))
        if job["callback_id"]:
            event_id = "ve_" + job["id"][3:]
            payload = canonical({"event_id": event_id, "job_id": job["id"], "state": state,
                                 "receipt_id": job["receipt_id"] if state in {"COMPLETE", "PARTIAL_FAILURE"} else None,
                                 "finished_at": now})
            self.execute(con, """INSERT INTO verification_callback_outbox
                (tenant_id,job_id,event_id,callback_id,callback_fingerprint,payload_json,state,next_attempt_at,deadline_at)
                VALUES(?,?,?,?,?,?,'PENDING',?,?) ON CONFLICT(tenant_id,job_id) DO NOTHING""",
                (job["tenant_id"], job["id"], event_id, job["callback_id"], job["callback_fingerprint"], payload, now, now + 86400))
        return updated

    def get_job(self, tenant, job_id):
        with self.transaction() as con:
            row = self.row(con, tenant, job_id, lock=True)
            if row and row["state"] not in TERMINAL and row["deadline_at"] <= self.now(con):
                row = self._terminal(con, row, "EXPIRED", "deadline_exceeded", self.now(con))
            return row

    def cancel(self, tenant, job_id):
        with self.transaction() as con:
            row = self.row(con, tenant, job_id, lock=True)
            return self._terminal(con, row, "EXPIRED", "cancelled", self.now(con)) if row else None

    def public(self, row):
        if not row:
            return None
        with self.transaction() as con:
            counts = self.execute(con, """SELECT state,COUNT(*) AS n FROM verification_conditions
                WHERE tenant_id=? AND job_id=? GROUP BY state""", (row["tenant_id"], row["id"])).fetchall()
            callback = self._row(self.execute(con, """SELECT state,attempts,error_code FROM verification_callback_outbox
                WHERE tenant_id=? AND job_id=?""", (row["tenant_id"], row["id"])))
        fields = ("id", "state", "revision", "assurance_level", "condition_count", "created_at", "started_at",
                  "finished_at", "deadline_at", "terminal_reason")
        return {**{key: row[key] for key in fields}, "conditions": {r["state"]: r["n"] for r in counts},
                "receipt_id": row["receipt_id"] if row["state"] in {"COMPLETE", "PARTIAL_FAILURE"} else None,
                "callback": callback}

    def conditions(self, tenant, job_id, *, public=False, offset=0, limit=1000):
        with self.transaction() as con:
            rows = [dict(r) for r in self.execute(con, """SELECT * FROM verification_conditions
                WHERE tenant_id=? AND job_id=? ORDER BY ordinal LIMIT ? OFFSET ?""", (tenant, job_id, limit, offset)).fetchall()]
            if not public:
                return rows
            attempts = [dict(r) for r in self.execute(con, """SELECT condition_id,attempt,started_at,finished_at,
                outcome,error_code,next_attempt_at FROM verification_attempts WHERE tenant_id=? AND job_id=?
                ORDER BY condition_id,attempt""", (tenant, job_id)).fetchall()]
        return [{"id": r["condition_id"], "provider": r["provider"], "state": r["state"], "attempts": r["attempts"],
                 "next_attempt_at": r["next_attempt_at"] if r["state"] == "PENDING" else None,
                 "error_code": r["error_code"], "result": json.loads(r["result_json"]) if r["result_json"] else None,
                 "executions": [a for a in attempts if a["condition_id"] == r["condition_id"]]} for r in rows]

    def _owned(self, con, job):
        current = self.row(con, job["tenant_id"], job["id"], lock=True)
        now = self.now(con)
        if current["state"] in TERMINAL:
            return None
        if current["deadline_at"] <= now:
            self._terminal(con, current, "EXPIRED", "deadline_exceeded", now)
            return None
        if current["lease_token"] != job["lease_token"] or current["lease_until"] <= now:
            return None
        return current

    def active(self, job):
        with self.transaction() as con:
            return bool(self._owned(con, job))

    def claim(self, lease_seconds):
        with self.transaction() as con:
            now = self.now(con)
            job = self._row(self.execute(con, """SELECT * FROM verification_jobs WHERE finished_at IS NULL
                AND ((next_run_at<=? AND lease_until<=?) OR deadline_at<=?)
                ORDER BY next_run_at,created_at LIMIT 1""" + self.lock(skip=True), (now, now, now)))
            if not job:
                return None
            if job["deadline_at"] <= now:
                self._terminal(con, job, "EXPIRED", "deadline_exceeded", now)
                return None
            if not self.providers_current(job):
                self._terminal(con, job, "INTERNAL_ERROR", "provider_definition_changed", now)
                return None
            token = uuid4().hex
            self.execute(con, """UPDATE verification_jobs SET lease_token=?,lease_until=?,
                state=CASE WHEN state='QUEUED' THEN 'OBSERVING' ELSE state END,
                started_at=COALESCE(started_at,?),revision=revision+1 WHERE tenant_id=? AND id=?""",
                (token, now + lease_seconds, now, job["tenant_id"], job["id"]))
            interrupted = self.execute(con, """SELECT * FROM verification_conditions
                WHERE tenant_id=? AND job_id=? AND state='RUNNING'""", (job["tenant_id"], job["id"])).fetchall()
            for condition in interrupted:
                exhausted = condition["attempts"] >= self.registry.policy(condition["provider"]).attempts
                observation = ObservationRecord(indeterminate=True, note="Provider observation was interrupted before authoritative state was established.")
                self.execute(con, """UPDATE verification_conditions SET state=?,lease_token=NULL,observation_json=?,
                    infrastructure_failure=?,error_code='worker_interrupted',next_attempt_at=?
                    WHERE tenant_id=? AND job_id=? AND condition_id=?""",
                    ("OBSERVED" if exhausted else "PENDING", observation.model_dump_json() if exhausted else None,
                     int(exhausted), now, job["tenant_id"], job["id"], condition["condition_id"]))
                self.execute(con, """UPDATE verification_attempts SET finished_at=?,outcome='interrupted',error_code='worker_interrupted'
                    WHERE tenant_id=? AND job_id=? AND condition_id=? AND finished_at IS NULL""",
                    (now, job["tenant_id"], job["id"], condition["condition_id"]))
            return self.row(con, job["tenant_id"], job["id"])

    def claim_conditions(self, job, batch_size, lease_seconds):
        with self.transaction() as con:
            if not self._owned(con, job):
                return []
            now, claimed = self.now(con), []
            rows = self.execute(con, """SELECT * FROM verification_conditions WHERE tenant_id=? AND job_id=?
                AND state='PENDING' AND next_attempt_at<=? ORDER BY ordinal""", (job["tenant_id"], job["id"], now)).fetchall()
            saturated = set()
            for row in rows:
                if row["provider"] in saturated:
                    continue
                slot = self._row(self.execute(con, """SELECT * FROM verification_provider_slots
                    WHERE provider=? AND lease_until<=? AND slot<? ORDER BY slot LIMIT 1""" + self.lock(skip=True), (row["provider"], now, self.registry.concurrency().get(row["provider"], 0))))
                if not slot:
                    saturated.add(row["provider"])
                    continue
                token = uuid4().hex
                self.execute(con, "UPDATE verification_provider_slots SET lease_token=?,lease_until=? WHERE provider=? AND slot=?",
                             (token, now + lease_seconds, row["provider"], slot["slot"]))
                self.execute(con, """UPDATE verification_conditions SET state='RUNNING',attempts=attempts+1,lease_token=?
                    WHERE tenant_id=? AND job_id=? AND condition_id=?""", (token, job["tenant_id"], job["id"], row["condition_id"]))
                self.execute(con, """INSERT INTO verification_attempts(tenant_id,job_id,condition_id,attempt,started_at)
                    VALUES(?,?,?,?,?)""", (job["tenant_id"], job["id"], row["condition_id"], row["attempts"] + 1, now))
                claimed.append({**dict(row), "attempts": row["attempts"] + 1, "lease_token": token, "slot": slot["slot"]})
                if len(claimed) >= batch_size:
                    break
            return claimed

    def release_slots(self, claims):
        with self.transaction() as con:
            for claim in claims:
                self.execute(con, """UPDATE verification_provider_slots SET lease_token=NULL,lease_until=0
                    WHERE provider=? AND slot=? AND lease_token=?""", (claim["provider"], claim["slot"], claim["lease_token"]))

    def finish_observations(self, job, outcomes):
        """Checkpoint a batch and release its job lease, including the next durable wake time."""
        with self.transaction() as con:
            current = self._owned(con, job)
            if not current:
                return
            now = self.now(con)
            for claim, observation, failure, delay in outcomes:
                if observation:
                    observation = observation.checkpoint()
                retry = failure is not None and claim["attempts"] < self.registry.policy(claim["provider"]).attempts
                if failure and not retry:
                    observation = ObservationRecord(indeterminate=True,
                        note="Provider remained unavailable after the bounded retry policy was exhausted.")
                self.execute(con, """UPDATE verification_conditions SET state=?,lease_token=NULL,observation_json=?,
                    infrastructure_failure=?,error_code=?,next_attempt_at=?
                    WHERE tenant_id=? AND job_id=? AND condition_id=? AND lease_token=?""",
                    ("PENDING" if retry else "OBSERVED", observation.model_dump_json() if observation else None,
                     int(bool(failure and not retry)), failure.code if failure else None,
                     now + delay if retry else now, job["tenant_id"], job["id"], claim["condition_id"], claim["lease_token"]))
                self.execute(con, """UPDATE verification_attempts SET finished_at=?,outcome=?,error_code=?,next_attempt_at=?
                    WHERE tenant_id=? AND job_id=? AND condition_id=? AND attempt=? AND finished_at IS NULL""",
                    (now, "retry" if retry else "exhausted" if failure else "observed", failure.code if failure else None,
                     now + delay if retry else None, job["tenant_id"], job["id"], claim["condition_id"], claim["attempts"]))
            pending = self.execute(con, """SELECT MIN(next_attempt_at) AS next,COUNT(*) AS n FROM verification_conditions
                WHERE tenant_id=? AND job_id=? AND state IN ('PENDING','RUNNING')""", (job["tenant_id"], job["id"])).fetchone()
            state = "OBSERVING" if pending["n"] else "EVALUATING"
            wake = min(current["deadline_at"], max(now + (0 if outcomes else 0.05), pending["next"] or now)) if pending["n"] else now
            self.execute(con, """UPDATE verification_jobs SET state=?,next_run_at=?,lease_token=NULL,lease_until=0,
                revision=revision+1 WHERE tenant_id=? AND id=?""", (state, wake, job["tenant_id"], job["id"]))

    def save_evaluation(self, job, receipt, signer_key_id):
        with self.transaction() as con:
            if not self._owned(con, job):
                return
            self.execute_many(con, """UPDATE verification_conditions SET result_json=?,state='EVALUATED'
                WHERE tenant_id=? AND job_id=? AND condition_id=?""",
                [(result.model_dump_json(), job["tenant_id"], job["id"], result.id) for result in receipt.results])
            self.execute(con, """UPDATE verification_jobs SET state='SIGNING',unsigned_receipt_json=?,signer_key_id=?,
                next_run_at=?,lease_token=NULL,lease_until=0,revision=revision+1 WHERE tenant_id=? AND id=?""",
                (receipt.model_dump_json(), signer_key_id, self.now(con), job["tenant_id"], job["id"]))

    def publish(self, job, engine):
        # One transaction serializes cancellation, connection changes, receipt insert and callback enqueue.
        from .connections import ManagedAdapter
        with self.transaction() as con:
            current = self._owned(con, job)
            if not current:
                return
            if current["signer_key_id"] != engine.signer.key_id:
                self._terminal(con, current, "INTERNAL_ERROR", "signing_key_changed", self.now(con))
                return
            contract = JobContract.model_validate_json(current["contract_json"])
            receipt = VerificationReceipt.model_validate_json(current["unsigned_receipt_json"])
            rows = self.execute(con, "SELECT * FROM verification_conditions WHERE tenant_id=? AND job_id=? ORDER BY ordinal",
                                (job["tenant_id"], job["id"])).fetchall()
            connections = {r["provider"]: r for r in self.execute(con,
                "SELECT * FROM connections WHERE tenant_id=? ORDER BY provider" + (" FOR SHARE" if self.pg else ""),
                (job["tenant_id"],)).fetchall()}
            changed = False
            for index, (pc, row) in enumerate(zip(contract.postconditions, rows, strict=True)):
                if not isinstance(engine.adapters.get(pc.provider), ManagedAdapter):
                    continue
                observation = ObservationRecord.model_validate_json(row["observation_json"])
                authority, connection = observation.authority or {}, connections.get(pc.provider)
                valid = ((authority.get("mode") == "public" and self.registry.require(pc.provider).manifest.authentication.public_read and connection is None)
                         or (authority.get("mode") == "managed" and connection and connection["state"] == "connected"
                             and connection["id"] == authority.get("connection_id")
                             and connection["revision"] == authority.get("revision")
                             and (connection["expires_at"] is None or connection["expires_at"] > self.now(con))))
                if not valid and not observation.indeterminate:
                    changed = True
                    observation.indeterminate = True
                    observation.state = None
                    observation.note = "Workspace connection changed before receipt publication; authoritative state is unknown."
                    receipt.results[index] = engine.evaluate_observation(pc, contract, observation)
            if changed:
                results = engine.evaluate_transitions(contract, receipt.results, current["assurance_level"],
                    {k: ConditionResult.model_validate(v) for k, v in json.loads(current["baselines_json"]).items()})
                receipt = engine.build_receipt(contract, results, assurance_level=current["assurance_level"],
                    receipt_id=current["receipt_id"], verified_at=receipt.verified_at, duration_ms=receipt.duration_ms)
            from .recovery_store import RecoveryStore
            recovery = RecoveryStore(self.store)
            link = recovery.prepare_publication(con, current, receipt)
            receipt = engine.sign(receipt)
            # Deadline can elapse while CPU work/signing runs; never publish after it.
            now = self.now(con)
            if now >= current["deadline_at"]:
                self._terminal(con, current, "EXPIRED", "deadline_exceeded", now)
                return
            self.execute(con, """INSERT INTO receipts(receipt_id,contract_id,verdict,body_json,verified_at,receipt_hash,signature,tenant_id)
                VALUES(?,?,?,?,?,?,?,?)""", (receipt.receipt_id, receipt.contract_id, receipt.verdict.value,
                    receipt.model_dump_json(), receipt.verified_at.isoformat(), receipt.receipt_hash, receipt.signature, job["tenant_id"]))
            if link:
                recovery.finish_publication(con, current, receipt, link)
            if changed:
                for result in receipt.results:
                    self.execute(con, "UPDATE verification_conditions SET result_json=? WHERE tenant_id=? AND job_id=? AND condition_id=?",
                                 (result.model_dump_json(), job["tenant_id"], job["id"], result.id))
            state = "PARTIAL_FAILURE" if any(r["infrastructure_failure"] for r in rows) else "COMPLETE"
            self._terminal(con, current, state, "provider_retries_exhausted" if state == "PARTIAL_FAILURE" else None, now)

    def fail_job(self, job):
        with self.transaction() as con:
            current = self._owned(con, job)
            if current:
                self._terminal(con, current, "INTERNAL_ERROR", "internal_error", self.now(con))

    def abandon(self, job):
        # Only call after our observation tasks have stopped; a crashed process simply loses its lease.
        with self.transaction() as con:
            self.execute(con, """UPDATE verification_jobs SET lease_until=0,next_run_at=?
                WHERE tenant_id=? AND id=? AND lease_token=? AND finished_at IS NULL""",
                (self.now(con), job["tenant_id"], job["id"], job["lease_token"]))

    def claim_callback(self):
        with self.transaction() as con:
            now = self.now(con)
            row = self._row(self.execute(con, """SELECT * FROM verification_callback_outbox
                WHERE state IN ('PENDING','SENDING') AND ((next_attempt_at<=? AND lease_until<=?) OR deadline_at<=?)
                ORDER BY next_attempt_at LIMIT 1""" + self.lock(skip=True), (now, now, now)))
            if not row:
                return None
            from .retries import CALLBACK_POLICY
            if row["attempts"] >= CALLBACK_POLICY.attempts or now >= row["deadline_at"]:
                self.execute(con, """UPDATE verification_callback_outbox SET state='DEAD',error_code='delivery_exhausted'
                    WHERE tenant_id=? AND job_id=?""", (row["tenant_id"], row["job_id"]))
                return None
            token = uuid4().hex
            self.execute(con, """UPDATE verification_callback_outbox SET state='SENDING',lease_token=?,
                lease_until=?,attempts=attempts+1 WHERE tenant_id=? AND job_id=?""", (token, now + 60, row["tenant_id"], row["job_id"]))
            return {**row, "lease_token": token, "attempts": row["attempts"] + 1}

    def finish_callback(self, row, state, error=None, delay=0):
        with self.transaction() as con:
            now = self.now(con)
            self.execute(con, """UPDATE verification_callback_outbox SET state=?,error_code=?,next_attempt_at=?,
                lease_token=NULL,lease_until=0 WHERE tenant_id=? AND job_id=? AND lease_token=? AND lease_until>?""",
                (state, error, now + delay, row["tenant_id"], row["job_id"], row["lease_token"], now))


def evaluation_inputs(job):
    return (JobContract.model_validate_json(job["contract_json"]),
            {k: ConditionResult.model_validate(v) for k, v in json.loads(job["baselines_json"]).items()},
            datetime.fromtimestamp(job["created_at"], timezone.utc))
