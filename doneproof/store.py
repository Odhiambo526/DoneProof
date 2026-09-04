from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .domain import CompletionContract, VerificationReceipt


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _add_column_if_missing(self, con: sqlite3.Connection, table: str, column: str, ddl: str):
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    def _init(self):
        with self._connect() as con:
            con.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS contracts (
                    id TEXT NOT NULL,
                    task TEXT NOT NULL,
                    body_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    PRIMARY KEY(tenant_id, id)
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    receipt_id TEXT PRIMARY KEY,
                    contract_id TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    body_json TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    receipt_hash TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default'
                );
                CREATE TABLE IF NOT EXISTS contract_baselines (
                    tenant_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id, contract_id, condition_id)
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    tenant_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    receipt_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_id TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence_events (
                    event_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    object_id TEXT,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );
                """
            )
            self._add_column_if_missing(con, "contracts", "tenant_id", "tenant_id TEXT NOT NULL DEFAULT 'default'")
            self._add_column_if_missing(con, "receipts", "tenant_id", "tenant_id TEXT NOT NULL DEFAULT 'default'")
            self._migrate_contract_primary_key(con)
            con.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_receipts_tenant_time ON receipts(tenant_id, verified_at DESC);
                CREATE INDEX IF NOT EXISTS idx_receipts_contract ON receipts(tenant_id, contract_id, verified_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_lookup ON evidence_events(tenant_id, source, event_type, object_id, occurred_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_tenant_time ON audit_events(tenant_id, created_at DESC);
                """
            )

    def _migrate_contract_primary_key(self, con: sqlite3.Connection) -> None:
        """Upgrade legacy global contract IDs to tenant-scoped IDs without losing data."""
        info = con.execute("PRAGMA table_info(contracts)").fetchall()
        pk_cols = [row[1] for row in sorted((r for r in info if r[5]), key=lambda r: r[5])]
        if pk_cols != ["id"]:
            return
        con.executescript(
            """
            ALTER TABLE contracts RENAME TO contracts_legacy_global_pk;
            CREATE TABLE contracts (
                id TEXT NOT NULL,
                task TEXT NOT NULL,
                body_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                PRIMARY KEY(tenant_id, id)
            );
            INSERT INTO contracts(id,task,body_json,created_at,tenant_id)
                SELECT id,task,body_json,created_at,tenant_id FROM contracts_legacy_global_pk;
            DROP TABLE contracts_legacy_global_pk;
            """
        )

    def ping(self) -> bool:
        with self._connect() as con:
            return con.execute("SELECT 1").fetchone()[0] == 1

    def save_contract(self, tenant_id: str, contract: CompletionContract):
        body = contract.model_dump_json()
        with self._connect() as con:
            existing = con.execute(
                "SELECT body_json FROM contracts WHERE tenant_id=? AND id=?", (tenant_id, contract.id)
            ).fetchone()
            if existing:
                if existing[0] != body:
                    raise ValueError("contract id already exists with different content")
                return
            con.execute(
                "INSERT INTO contracts(id, task, body_json, created_at, tenant_id) VALUES(?,?,?,?,?)",
                (contract.id, contract.task, body, contract.created_at.isoformat(), tenant_id),
            )

    def save_receipt(self, tenant_id: str, receipt: VerificationReceipt):
        body = receipt.model_dump_json()
        with self._connect() as con:
            con.execute(
                "INSERT INTO receipts(receipt_id, contract_id, verdict, body_json, verified_at, receipt_hash, signature, tenant_id) VALUES(?,?,?,?,?,?,?,?)",
                (
                    receipt.receipt_id,
                    receipt.contract_id,
                    receipt.verdict.value,
                    body,
                    receipt.verified_at.isoformat(),
                    receipt.receipt_hash,
                    receipt.signature,
                    tenant_id,
                ),
            )

    def get_contract(self, tenant_id: str, contract_id: str) -> CompletionContract | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT body_json FROM contracts WHERE tenant_id=? AND id=?", (tenant_id, contract_id)
            ).fetchone()
        return CompletionContract.model_validate_json(row[0]) if row else None

    def get_receipt(self, tenant_id: str, receipt_id: str) -> VerificationReceipt | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT body_json FROM receipts WHERE tenant_id=? AND receipt_id=?", (tenant_id, receipt_id)
            ).fetchone()
        return VerificationReceipt.model_validate_json(row[0]) if row else None

    def list_receipts(self, tenant_id: str, limit: int = 50) -> list[VerificationReceipt]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT body_json FROM receipts WHERE tenant_id=? ORDER BY verified_at DESC LIMIT ?",
                (tenant_id, max(1, min(limit, 200))),
            ).fetchall()
        return [VerificationReceipt.model_validate_json(r[0]) for r in rows]

    def stats(self, tenant_id: str) -> dict[str, Any]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT verdict, COUNT(*) c FROM receipts WHERE tenant_id=? GROUP BY verdict", (tenant_id,)
            ).fetchall()
            total = con.execute("SELECT COUNT(*) FROM receipts WHERE tenant_id=?", (tenant_id,)).fetchone()[0]
        counts = {"VERIFIED": 0, "PARTIAL": 0, "FAILED": 0, "UNKNOWN": 0}
        for row in rows:
            counts[row[0]] = row[1]
        counts["total"] = total
        counts["verification_rate"] = round((counts["VERIFIED"] / total * 100), 1) if total else 0.0
        with self._connect() as con:
            recent = con.execute(
                "SELECT body_json FROM receipts WHERE tenant_id=? ORDER BY verified_at DESC LIMIT 200", (tenant_id,)
            ).fetchall()
        durations: list[float] = []
        providers: dict[str, int] = {}
        for row in recent:
            try:
                body = json.loads(row[0])
                durations.append(float(body.get("duration_ms") or 0))
                for provider in (body.get("summary") or {}).get("providers", []):
                    providers[str(provider)] = providers.get(str(provider), 0) + 1
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
        counts["average_duration_ms"] = round(sum(durations) / len(durations), 2) if durations else 0.0
        counts["providers"] = providers
        return counts



    def save_baseline(self, tenant_id: str, contract_id: str, result) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO contract_baselines(tenant_id,contract_id,condition_id,result_json,captured_at) VALUES(?,?,?,?,?)",
                (tenant_id, contract_id, result.id, result.model_dump_json(), datetime.now(timezone.utc).isoformat()),
            )

    def get_baselines(self, tenant_id: str, contract_id: str):
        from .domain import ConditionResult
        with self._connect() as con:
            rows = con.execute(
                "SELECT condition_id,result_json FROM contract_baselines WHERE tenant_id=? AND contract_id=?",
                (tenant_id, contract_id),
            ).fetchall()
        return {row[0]: ConditionResult.model_validate_json(row[1]) for row in rows}

    def get_idempotency(self, tenant_id: str, key: str) -> dict[str, str] | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT request_hash, receipt_id FROM idempotency WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, key),
            ).fetchone()
        return {"request_hash": row[0], "receipt_id": row[1]} if row else None

    def save_idempotency(self, tenant_id: str, key: str, request_hash: str, receipt_id: str) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO idempotency(tenant_id,idempotency_key,request_hash,receipt_id,created_at) VALUES(?,?,?,?,?)",
                (tenant_id, key, request_hash, receipt_id, datetime.now(timezone.utc).isoformat()),
            )

    def save_event(
        self,
        tenant_id: str,
        source: str,
        event_type: str,
        object_id: str | None,
        occurred_at: datetime,
        payload: Any,
        event_id: str,
    ) -> tuple[bool, str]:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        payload_hash = hashlib.sha256(canonical.encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            cur = con.execute(
                "INSERT OR IGNORE INTO evidence_events(event_id,tenant_id,source,event_type,object_id,occurred_at,payload_json,payload_hash,received_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, tenant_id, source, event_type, object_id, occurred_at.isoformat(), canonical, payload_hash, now),
            )
        return cur.rowcount == 1, payload_hash

    def find_events(
        self,
        tenant_id: str,
        source: str,
        event_type: str,
        object_id: str | None,
        occurred_after: datetime,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM evidence_events WHERE tenant_id=? AND source=? AND event_type=? AND occurred_at>=?"
        params: list[Any] = [tenant_id, source, event_type, occurred_after.isoformat()]
        if object_id is not None:
            sql += " AND object_id=?"
            params.append(object_id)
        sql += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(max(1, min(limit, 100)))
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append({
                "event_id": row["event_id"],
                "source": row["source"],
                "event_type": row["event_type"],
                "object_id": row["object_id"],
                "occurred_at": row["occurred_at"],
                "payload": json.loads(row["payload_json"]),
                "payload_hash": row["payload_hash"],
            })
        return out

    def audit(self, tenant_id: str, action: str, object_type: str, object_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        body = json.dumps(metadata or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self._connect() as con:
            con.execute(
                "INSERT INTO audit_events(tenant_id,action,object_type,object_id,metadata_json,created_at) VALUES(?,?,?,?,?,?)",
                (tenant_id, action, object_type, object_id, body, datetime.now(timezone.utc).isoformat()),
            )

    def list_audit(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT action,object_type,object_id,metadata_json,created_at FROM audit_events WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        return [
            {"action": r[0], "object_type": r[1], "object_id": r[2], "metadata": json.loads(r[3]), "created_at": r[4]}
            for r in rows
        ]
