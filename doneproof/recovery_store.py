"""Tenant-scoped, linear receipt chains and durable evidence-triggered re-verification.

Recovery only schedules observation jobs. It has no provider execution interface.
Lock order for admission is tenant -> chain -> new job; publication locks its
existing job -> chain. Admission never locks an existing job while holding a chain.
"""
import json
from datetime import datetime, timezone

from .domain import ConditionResult, VerificationReceipt
from .job_models import JobContract
from .job_store import IdempotencyConflict, JobStore, QueueFull, canonical, digest
from .recovery_models import RecoveryInfo
from .remediation import contains_guidance, failure_patterns, remediation_for
from .signing import ReceiptSigner


class RecoveryError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


class RecoveryStore(JobStore):
    def __init__(self, store, max_attempts=20):
        super().__init__(store)
        if type(max_attempts) is not int or not 0 <= max_attempts <= 20:
            raise ValueError("Re-verification limit must be an integer between 0 and 20")
        self.max_attempts = max_attempts

    def receipt(self, con, tenant, receipt_id):
        row = self._row(self.execute(con, "SELECT body_json FROM receipts WHERE tenant_id=? AND receipt_id=?",
                                    (tenant, receipt_id)))
        if not row:
            raise RecoveryError("receipt_not_found", 404)
        receipt = VerificationReceipt.model_validate_json(row["body_json"])
        if not ReceiptSigner.verify(receipt):
            raise RecoveryError("receipt_integrity_failed")
        return receipt

    def chain(self, con, tenant, root, *, lock=True):
        return self._row(self.execute(con, "SELECT * FROM recovery_chains WHERE tenant_id=? AND root_id=?" +
                                     (self.lock() if lock else ""), (tenant, root)))

    def ensure(self, con, tenant, receipt_id):
        # Serialize lazy enrollment with all admissions in this workspace.
        self.execute(con, "INSERT INTO verification_tenants(tenant_id) VALUES(?) ON CONFLICT DO NOTHING", (tenant,))
        self.execute(con, "SELECT tenant_id FROM verification_tenants WHERE tenant_id=?" + self.lock(), (tenant,))
        receipt = self.receipt(con, tenant, receipt_id)
        node = self._row(self.execute(con, "SELECT root_id FROM recovery_nodes WHERE tenant_id=? AND receipt_id=?",
                                     (tenant, receipt_id)))
        if node:
            return self.chain(con, tenant, node["root_id"])
        if receipt.previous_receipt_id:
            raise RecoveryError("receipt_chain_incomplete")
        # Previously issued receipts are enrolled without rewriting or re-signing them.
        job = self._row(self.execute(con, """SELECT contract_json FROM verification_jobs
            WHERE tenant_id=? AND receipt_id=? AND state IN ('COMPLETE','PARTIAL_FAILURE')""", (tenant, receipt_id)))
        stored = job or self._row(self.execute(con, "SELECT body_json AS contract_json FROM contracts WHERE tenant_id=? AND id=?",
                                               (tenant, receipt.contract_id)))
        if not stored:
            raise RecoveryError("original_contract_unavailable")
        contract = JobContract.model_validate_json(stored["contract_json"])
        if digest(canonical(contract.model_dump(mode="json"))) != receipt.contract_hash:
            raise RecoveryError("original_contract_mismatch")
        # Reconstruct only what the original signed receipt attests about its
        # pre-execution baseline. Never recapture state after a repair.
        baselines = {}
        for result in receipt.results:
            if result.transition_required and result.baseline_status is not None:
                baseline = result.model_copy(deep=True)
                baseline.status = result.baseline_status
                baseline.evidence.observed = result.baseline_observed
                baselines[result.id] = baseline.model_dump(mode="json")
        self.execute(con, """INSERT INTO recovery_snapshots
            (tenant_id,root_id,contract_json,baselines_json,assurance_level,created_at) VALUES(?,?,?,?,?,?)""",
            (tenant, receipt_id, contract.model_dump_json(), canonical(baselines), receipt.assurance_level, self.now(con)))
        self.execute(con, """INSERT INTO recovery_nodes(tenant_id,root_id,receipt_id,ordinal,receipt_hash)
            VALUES(?,?,?,0,?)""", (tenant, receipt_id, receipt_id, receipt.receipt_hash))
        self.execute(con, """INSERT INTO recovery_chains(tenant_id,root_id,head_id,max_attempts)
            VALUES(?,?,?,?)""", (tenant, receipt_id, receipt_id, self.max_attempts))
        for pc in contract.postconditions:
            if pc.provider == "webhook" and all(isinstance(pc.selector.get(k), str) and pc.selector[k]
                                               for k in ("source", "event_type", "object_id")):
                self.execute(con, """INSERT INTO recovery_watches
                    (tenant_id,root_id,condition_id,source,event_type,object_id) VALUES(?,?,?,?,?,?)""",
                    (tenant, receipt_id, pc.id, pc.selector["source"], pc.selector["event_type"], pc.selector["object_id"]))
        return self.chain(con, tenant, receipt_id)

    def chain_receipts(self, con, tenant, root):
        rows = self.execute(con, """SELECT r.body_json,n.receipt_hash FROM recovery_nodes n
            JOIN receipts r ON r.tenant_id=n.tenant_id AND r.receipt_id=n.receipt_id
            WHERE n.tenant_id=? AND n.root_id=? ORDER BY n.ordinal""", (tenant, root)).fetchall()
        receipts = []
        previous = None
        for row in rows:
            receipt = VerificationReceipt.model_validate_json(row["body_json"])
            if (not ReceiptSigner.verify(receipt) or receipt.receipt_hash != row["receipt_hash"]
                    or (previous and (receipt.previous_receipt_id != previous.receipt_id
                                      or receipt.previous_receipt_hash != previous.receipt_hash))):
                raise RecoveryError("receipt_chain_integrity_failed")
            receipts.append(receipt)
            previous = receipt
        return receipts

    def _admit(self, con, chain, receipt_id, key, request_hash, deadline, callback=None, event_id=None):
        tenant, root = chain["tenant_id"], chain["root_id"]
        existing = self._row(self.execute(con,
            "SELECT * FROM verification_jobs WHERE tenant_id=? AND idempotency_hash=?", (tenant, digest(key))))
        if existing:
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflict
            return existing, False
        if chain["head_id"] != receipt_id:
            raise RecoveryError("receipt_is_not_chain_head")
        if chain["active_job_id"]:
            raise RecoveryError("reverification_in_progress")
        if chain["attempts"] >= min(chain["max_attempts"], self.max_attempts):
            raise RecoveryError("reverification_limit_reached")
        previous = self.receipt(con, tenant, receipt_id)
        if previous.verdict == "VERIFIED":
            raise RecoveryError("receipt_already_verified")
        snapshot = self._row(self.execute(con, "SELECT * FROM recovery_snapshots WHERE tenant_id=? AND root_id=?",
                                         (tenant, root)))
        row, created = self.create_in_transaction(con, tenant, key, request_hash,
            JobContract.model_validate_json(snapshot["contract_json"]),
            {k: ConditionResult.model_validate(v) for k, v in json.loads(snapshot["baselines_json"]).items()},
            snapshot["assurance_level"], deadline, callback)
        attempt = chain["attempts"] + 1
        self.execute(con, """INSERT INTO recovery_attempts
            (tenant_id,root_id,attempt,job_id,previous_receipt_id,event_id,created_at) VALUES(?,?,?,?,?,?,?)""",
            (tenant, root, attempt, row["id"], receipt_id, event_id, self.now(con)))
        self.execute(con, "UPDATE recovery_chains SET attempts=?,active_job_id=? WHERE tenant_id=? AND root_id=?",
                     (attempt, row["id"], tenant, root))
        return row, created

    def reverify(self, tenant, receipt_id, key, request_hash, deadline=300, callback=None):
        with self.transaction() as con:
            chain = self.ensure(con, tenant, receipt_id)
            return self._admit(con, chain, receipt_id, "reverify:" + receipt_id + ":" + key, request_hash, deadline, callback)

    def prepare_publication(self, con, job, receipt):
        attempt = self._row(self.execute(con, "SELECT * FROM recovery_attempts WHERE tenant_id=? AND job_id=?",
                                        (job["tenant_id"], job["id"])))
        if not attempt:
            return None
        chain = self.chain(con, job["tenant_id"], attempt["root_id"])
        snapshot = self._row(self.execute(con, "SELECT * FROM recovery_snapshots WHERE tenant_id=? AND root_id=?",
                                         (job["tenant_id"], attempt["root_id"])))
        if (chain["active_job_id"] != job["id"] or chain["head_id"] != attempt["previous_receipt_id"]
                or any(job[k] != snapshot[k] for k in ("contract_json", "baselines_json", "assurance_level"))):
            raise RecoveryError("reverification_snapshot_changed")
        history = self.chain_receipts(con, job["tenant_id"], attempt["root_id"])
        previous = history[-1]
        receipt.previous_receipt_id = previous.receipt_id
        receipt.previous_receipt_hash = previous.receipt_hash
        oscillating, repeated = failure_patterns([*history, receipt])
        # Latch discovered oscillations so automatic recovery cannot resume a known cycle.
        oscillating = sorted(set(oscillating).union(*(r.recovery.oscillating_conditions for r in history if r.recovery)))
        receipt.recovery = RecoveryInfo(chain_id=attempt["root_id"], attempt=attempt["attempt"],
                                       oscillating_conditions=oscillating, repeated_failures=repeated)
        return {**attempt, "ordinal": len(history)}

    def finish_publication(self, con, job, receipt, link):
        self.execute(con, """INSERT INTO recovery_nodes
            (tenant_id,root_id,receipt_id,ordinal,previous_receipt_id,receipt_hash) VALUES(?,?,?,?,?,?)""",
            (job["tenant_id"], link["root_id"], receipt.receipt_id, link["ordinal"],
             receipt.previous_receipt_id, receipt.receipt_hash))
        self.execute(con, "UPDATE recovery_chains SET head_id=? WHERE tenant_id=? AND root_id=?",
                     (receipt.receipt_id, job["tenant_id"], link["root_id"]))

    def history(self, tenant, receipt_id):
        with self.transaction() as con:
            chain = self.ensure(con, tenant, receipt_id)
            receipts = self.chain_receipts(con, tenant, chain["root_id"])
            attempts = [dict(r) for r in self.execute(con, """SELECT a.attempt,a.job_id,a.previous_receipt_id,a.event_id,
                a.created_at,j.state,j.terminal_reason,j.finished_at FROM recovery_attempts a
                JOIN verification_jobs j ON j.tenant_id=a.tenant_id AND j.id=a.job_id
                WHERE a.tenant_id=? AND a.root_id=? ORDER BY a.attempt""", (tenant, chain["root_id"])).fetchall()]
            limit = min(chain["max_attempts"], self.max_attempts)
            head = receipts[-1]
            return {"chain_id": chain["root_id"], "head_id": chain["head_id"], "chain_integrity": True,
                "max_attempts": limit, "attempts_used": chain["attempts"], "active_job_id": chain["active_job_id"],
                "automatic": bool(chain["automatic"]), "can_reverify": not chain["active_job_id"]
                    and chain["attempts"] < limit and head.verdict != "VERIFIED",
                "attempts": attempts, "receipts": [{"receipt_id": r.receipt_id, "receipt_hash": r.receipt_hash,
                    "previous_receipt_id": r.previous_receipt_id, "verdict": r.verdict, "verified_at": r.verified_at.isoformat(),
                    "recovery": r.recovery.model_dump() if r.recovery else None,
                    "conditions": [{"condition": x.id, "status": x.status} for x in r.results],
                    "remediation": [x.model_dump() for x in (r.remediation if r.schema_version == "1.1" else remediation_for(r.results))]} for r in receipts]}

    def policy(self, tenant, receipt_id, automatic, configured_sources):
        with self.transaction() as con:
            chain = self.ensure(con, tenant, receipt_id)
            if automatic:
                watches = self.execute(con, "SELECT source FROM recovery_watches WHERE tenant_id=? AND root_id=?",
                                       (tenant, chain["root_id"])).fetchall()
                if not watches or any(w["source"] not in configured_sources for w in watches):
                    raise RecoveryError("automatic_reverification_requires_configured_exact_webhook_selectors", 422)
            self.execute(con, "UPDATE recovery_chains SET automatic=? WHERE tenant_id=? AND root_id=?",
                         (int(automatic), tenant, chain["root_id"]))

    def enqueue_event(self, con, tenant, source, event_type, object_id, event_id, payload):
        if contains_guidance(payload):
            return
        self.execute(con, """INSERT INTO recovery_event_queue(tenant_id,root_id,event_id,next_at)
            SELECT DISTINCT w.tenant_id,w.root_id,?,? FROM recovery_watches w
            JOIN recovery_chains c ON c.tenant_id=w.tenant_id AND c.root_id=w.root_id
            WHERE w.tenant_id=? AND w.source=? AND w.event_type=? AND w.object_id=?
                AND c.automatic=1 AND c.attempts<c.max_attempts
            ON CONFLICT(tenant_id,root_id,event_id) DO NOTHING""",
            (event_id, self.now(con), tenant, source, event_type, object_id))

    def dispatch_event(self):
        with self.transaction() as con:
            event = self._row(self.execute(con, """SELECT * FROM recovery_event_queue
                WHERE state='PENDING' AND next_at<=? ORDER BY next_at LIMIT 1""" + self.lock(skip=True), (self.now(con),)))
            if not event:
                return False
            tenant, root = event["tenant_id"], event["root_id"]
            chain = self.ensure(con, tenant, root)
            reason, job_id = None, None
            head = self.receipt(con, tenant, chain["head_id"])
            source_event = self._row(self.execute(con, "SELECT * FROM evidence_events WHERE tenant_id=? AND event_id=?",
                                                  (tenant, event["event_id"])))
            occurred = datetime.fromisoformat(source_event["occurred_at"]).astimezone(timezone.utc)
            # Compare against provider read time, not delayed receipt publication time.
            matching = [r for r in head.results if r.evidence.provider == "webhook"
                and all(r.evidence.selector.get(k) == source_event[k] for k in ("source", "event_type", "object_id"))]
            fresh = any(occurred > r.evidence.fetched_at for r in matching)
            if not chain["automatic"]:
                reason = "automatic_disabled"
            elif not fresh:
                reason = "evidence_not_newer_than_observation"
            elif head.recovery and (head.recovery.oscillating_conditions or head.recovery.repeated_failures):
                reason = "repeated_or_oscillating_failure"
            elif chain["active_job_id"]:
                self.execute(con, "UPDATE recovery_event_queue SET next_at=? WHERE tenant_id=? AND root_id=? AND event_id=?",
                             (self.now(con) + 1, tenant, root, event["event_id"]))
                return True
            else:
                try:
                    row, _ = self._admit(con, chain, chain["head_id"], "recovery-event:" + root + ":" + event["event_id"],
                                         digest(canonical({"root": root, "event_id": event["event_id"]})), 300,
                                         event_id=event["event_id"])
                    job_id = row["id"]
                except RecoveryError as exc:
                    reason = exc.code
                except QueueFull:
                    self.execute(con, "UPDATE recovery_event_queue SET next_at=? WHERE tenant_id=? AND root_id=? AND event_id=?",
                                 (self.now(con) + 30, tenant, root, event["event_id"]))
                    return True
            self.execute(con, """UPDATE recovery_event_queue SET state=?,reason=?,job_id=?
                WHERE tenant_id=? AND root_id=? AND event_id=?""",
                ("IGNORED" if reason else "DONE", reason, job_id, tenant, root, event["event_id"]))
            return True
