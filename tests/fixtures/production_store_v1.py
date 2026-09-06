from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .domain import CompletionContract, VerificationReceipt


def _is_postgres(dsn: str) -> bool:
    return dsn.startswith("postgresql://") or dsn.startswith("postgres://")


class Store:
    """Persistence facade supporting local SQLite and durable PostgreSQL.

    SQLite is intentionally retained for local development and tests. Production
    deployments should provide DATABASE_URL with a PostgreSQL connection string.
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.backend = "postgresql" if _is_postgres(dsn) else "sqlite"
        if self.backend == "sqlite":
            self.path = dsn
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._init_sqlite()
        else:
            self.path = None
            self._init_postgres()

    # ------------------------------- SQLite -------------------------------
    def _connect(self):
        """Return a SQLite connection. Kept public-ish for legacy migration tests."""
        if self.backend != "sqlite":
            raise RuntimeError("_connect() is only available for SQLite stores")
        con = sqlite3.connect(self.dsn, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _add_column_if_missing(self, con: sqlite3.Connection, table: str, column: str, ddl: str):
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    def _init_sqlite(self):
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

    # ----------------------------- PostgreSQL -----------------------------
    def _pg_connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - packaging/CI protects this path
            raise RuntimeError(
                "PostgreSQL storage requires psycopg; install the doneproof package dependencies"
            ) from exc
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=8)

    def _init_postgres(self) -> None:
        """Apply idempotent schema migrations safe for concurrent serverless starts."""
        statements = [
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS contracts (
                id TEXT NOT NULL,
                task TEXT NOT NULL,
                body_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                PRIMARY KEY(tenant_id, id)
            )""",
            """CREATE TABLE IF NOT EXISTS receipts (
                receipt_id TEXT PRIMARY KEY,
                contract_id TEXT NOT NULL,
                verdict TEXT NOT NULL,
                body_json TEXT NOT NULL,
                verified_at TEXT NOT NULL,
                receipt_hash TEXT NOT NULL,
                signature TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default'
            )""",
            """CREATE TABLE IF NOT EXISTS contract_baselines (
                tenant_id TEXT NOT NULL,
                contract_id TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                result_json TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id, contract_id, condition_id)
            )""",
            """CREATE TABLE IF NOT EXISTS idempotency (
                tenant_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                receipt_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id, idempotency_key)
            )""",
            """CREATE TABLE IF NOT EXISTS audit_events (
                id BIGSERIAL PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                action TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS evidence_events (
                event_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                source TEXT NOT NULL,
                event_type TEXT NOT NULL,
                object_id TEXT,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                received_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_receipts_tenant_time ON receipts(tenant_id, verified_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_receipts_contract ON receipts(tenant_id, contract_id, verified_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_events_lookup ON evidence_events(tenant_id, source, event_type, object_id, occurred_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_audit_tenant_time ON audit_events(tenant_id, created_at DESC)",
        ]
        with self._pg_connect() as con:
            with con.cursor() as cur:
                # Serialize first-time schema bootstrap across concurrent cold starts.
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (0x444F4E4550524F4F,))
                for statement in statements:
                    cur.execute(statement)
                cur.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(%s,%s) ON CONFLICT (version) DO NOTHING",
                    (1, datetime.now(timezone.utc).isoformat()),
                )

    # ------------------------------- Shared -------------------------------
    def ping(self) -> bool:
        if self.backend == "sqlite":
            with self._connect() as con:
                return con.execute("SELECT 1").fetchone()[0] == 1
        with self._pg_connect() as con:
            with con.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                return cur.fetchone()["ok"] == 1

    def save_contract(self, tenant_id: str, contract: CompletionContract):
        body = contract.model_dump_json()
        if self.backend == "sqlite":
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
            return
        with self._pg_connect() as con:
            with con.cursor() as cur:
                cur.execute("SELECT body_json FROM contracts WHERE tenant_id=%s AND id=%s", (tenant_id, contract.id))
                existing = cur.fetchone()
                if existing:
                    if existing["body_json"] != body:
                        raise ValueError("contract id already exists with different content")
                    return
                cur.execute(
                    "INSERT INTO contracts(id, task, body_json, created_at, tenant_id) VALUES(%s,%s,%s,%s,%s)",
                    (contract.id, contract.task, body, contract.created_at.isoformat(), tenant_id),
                )

    def save_receipt(self, tenant_id: str, receipt: VerificationReceipt):
        body = receipt.model_dump_json()
        values = (
            receipt.receipt_id,
            receipt.contract_id,
            receipt.verdict.value,
            body,
            receipt.verified_at.isoformat(),
            receipt.receipt_hash,
            receipt.signature,
            tenant_id,
        )
        if self.backend == "sqlite":
            with self._connect() as con:
                con.execute(
                    "INSERT INTO receipts(receipt_id, contract_id, verdict, body_json, verified_at, receipt_hash, signature, tenant_id) VALUES(?,?,?,?,?,?,?,?)",
                    values,
                )
            return
        with self._pg_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "INSERT INTO receipts(receipt_id, contract_id, verdict, body_json, verified_at, receipt_hash, signature, tenant_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    values,
                )

    def get_contract(self, tenant_id: str, contract_id: str) -> CompletionContract | None:
        if self.backend == "sqlite":
            with self._connect() as con:
                row = con.execute(
                    "SELECT body_json FROM contracts WHERE tenant_id=? AND id=?", (tenant_id, contract_id)
                ).fetchone()
            body = row[0] if row else None
        else:
            with self._pg_connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "SELECT body_json FROM contracts WHERE tenant_id=%s AND id=%s", (tenant_id, contract_id)
                    )
                    row = cur.fetchone()
            body = row["body_json"] if row else None
        return CompletionContract.model_validate_json(body) if body else None

    def get_receipt(self, tenant_id: str, receipt_id: str) -> VerificationReceipt | None:
        if self.backend == "sqlite":
            with self._connect() as con:
                row = con.execute(
                    "SELECT body_json FROM receipts WHERE tenant_id=? AND receipt_id=?", (tenant_id, receipt_id)
                ).fetchone()
            body = row[0] if row else None
        else:
            with self._pg_connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "SELECT body_json FROM receipts WHERE tenant_id=%s AND receipt_id=%s", (tenant_id, receipt_id)
                    )
                    row = cur.fetchone()
            body = row["body_json"] if row else None
        return VerificationReceipt.model_validate_json(body) if body else None

    def list_receipts(self, tenant_id: str, limit: int = 50) -> list[VerificationReceipt]:
        bounded = max(1, min(limit, 200))
        if self.backend == "sqlite":
            with self._connect() as con:
                rows = con.execute(
                    "SELECT body_json FROM receipts WHERE tenant_id=? ORDER BY verified_at DESC LIMIT ?",
                    (tenant_id, bounded),
                ).fetchall()
            bodies = [r[0] for r in rows]
        else:
            with self._pg_connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "SELECT body_json FROM receipts WHERE tenant_id=%s ORDER BY verified_at DESC LIMIT %s",
                        (tenant_id, bounded),
                    )
                    rows = cur.fetchall()
            bodies = [r["body_json"] for r in rows]
        return [VerificationReceipt.model_validate_json(body) for body in bodies]

    def stats(self, tenant_id: str) -> dict[str, Any]:
        if self.backend == "sqlite":
            with self._connect() as con:
                rows = con.execute(
                    "SELECT verdict, COUNT(*) c FROM receipts WHERE tenant_id=? GROUP BY verdict", (tenant_id,)
                ).fetchall()
                total = con.execute("SELECT COUNT(*) FROM receipts WHERE tenant_id=?", (tenant_id,)).fetchone()[0]
                recent = con.execute(
                    "SELECT body_json FROM receipts WHERE tenant_id=? ORDER BY verified_at DESC LIMIT 200", (tenant_id,)
                ).fetchall()
            grouped = [(row[0], row[1]) for row in rows]
            recent_bodies = [row[0] for row in recent]
        else:
            with self._pg_connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "SELECT verdict, COUNT(*) AS c FROM receipts WHERE tenant_id=%s GROUP BY verdict", (tenant_id,)
                    )
                    grouped = [(row["verdict"], row["c"]) for row in cur.fetchall()]
                    cur.execute("SELECT COUNT(*) AS c FROM receipts WHERE tenant_id=%s", (tenant_id,))
                    total = cur.fetchone()["c"]
                    cur.execute(
                        "SELECT body_json FROM receipts WHERE tenant_id=%s ORDER BY verified_at DESC LIMIT 200",
                        (tenant_id,),
                    )
                    recent_bodies = [row["body_json"] for row in cur.fetchall()]
        counts: dict[str, Any] = {"VERIFIED": 0, "PARTIAL": 0, "FAILED": 0, "UNKNOWN": 0}
        for verdict, count in grouped:
            counts[verdict] = count
        counts["total"] = total
        counts["verification_rate"] = round((counts["VERIFIED"] / total * 100), 1) if total else 0.0
        durations: list[float] = []
        providers: dict[str, int] = {}
        for body_json in recent_bodies:
            try:
                body = json.loads(body_json)
                durations.append(float(body.get("duration_ms") or 0))
                for provider in (body.get("summary") or {}).get("providers", []):
                    providers[str(provider)] = providers.get(str(provider), 0) + 1
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
        counts["average_duration_ms"] = round(sum(durations) / len(durations), 2) if durations else 0.0
        counts["providers"] = providers
        return counts

    def save_baseline(self, tenant_id: str, contract_id: str, result) -> None:
        values = (tenant_id, contract_id, result.id, result.model_dump_json(), datetime.now(timezone.utc).isoformat())
        if self.backend == "sqlite":
            with self._connect() as con:
                con.execute(
                    "INSERT OR REPLACE INTO contract_baselines(tenant_id,contract_id,condition_id,result_json,captured_at) VALUES(?,?,?,?,?)",
                    values,
                )
            return
        with self._pg_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """INSERT INTO contract_baselines(tenant_id,contract_id,condition_id,result_json,captured_at)
                    VALUES(%s,%s,%s,%s,%s)
                    ON CONFLICT (tenant_id,contract_id,condition_id)
                    DO UPDATE SET result_json=EXCLUDED.result_json,captured_at=EXCLUDED.captured_at""",
                    values,
                )

    def get_baselines(self, tenant_id: str, contract_id: str):
        from .domain import ConditionResult

        if self.backend == "sqlite":
            with self._connect() as con:
                rows = con.execute(
                    "SELECT condition_id,result_json FROM contract_baselines WHERE tenant_id=? AND contract_id=?",
                    (tenant_id, contract_id),
                ).fetchall()
            pairs = [(row[0], row[1]) for row in rows]
        else:
            with self._pg_connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "SELECT condition_id,result_json FROM contract_baselines WHERE tenant_id=%s AND contract_id=%s",
                        (tenant_id, contract_id),
                    )
                    rows = cur.fetchall()
            pairs = [(row["condition_id"], row["result_json"]) for row in rows]
        return {condition_id: ConditionResult.model_validate_json(body) for condition_id, body in pairs}

    def get_idempotency(self, tenant_id: str, key: str) -> dict[str, str] | None:
        if self.backend == "sqlite":
            with self._connect() as con:
                row = con.execute(
                    "SELECT request_hash, receipt_id FROM idempotency WHERE tenant_id=? AND idempotency_key=?",
                    (tenant_id, key),
                ).fetchone()
            return {"request_hash": row[0], "receipt_id": row[1]} if row else None
        with self._pg_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT request_hash, receipt_id FROM idempotency WHERE tenant_id=%s AND idempotency_key=%s",
                    (tenant_id, key),
                )
                row = cur.fetchone()
        return {"request_hash": row["request_hash"], "receipt_id": row["receipt_id"]} if row else None

    def save_idempotency(self, tenant_id: str, key: str, request_hash: str, receipt_id: str) -> None:
        values = (tenant_id, key, request_hash, receipt_id, datetime.now(timezone.utc).isoformat())
        if self.backend == "sqlite":
            with self._connect() as con:
                con.execute(
                    "INSERT INTO idempotency(tenant_id,idempotency_key,request_hash,receipt_id,created_at) VALUES(?,?,?,?,?)",
                    values,
                )
            return
        with self._pg_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "INSERT INTO idempotency(tenant_id,idempotency_key,request_hash,receipt_id,created_at) VALUES(%s,%s,%s,%s,%s)",
                    values,
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
        values = (
            event_id,
            tenant_id,
            source,
            event_type,
            object_id,
            occurred_at.isoformat(),
            canonical,
            payload_hash,
            now,
        )
        if self.backend == "sqlite":
            with self._connect() as con:
                cur = con.execute(
                    "INSERT OR IGNORE INTO evidence_events(event_id,tenant_id,source,event_type,object_id,occurred_at,payload_json,payload_hash,received_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    values,
                )
                inserted = cur.rowcount == 1
        else:
            with self._pg_connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        """INSERT INTO evidence_events(event_id,tenant_id,source,event_type,object_id,occurred_at,payload_json,payload_hash,received_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (event_id) DO NOTHING""",
                        values,
                    )
                    inserted = cur.rowcount == 1
        return inserted, payload_hash

    def find_events(
        self,
        tenant_id: str,
        source: str,
        event_type: str,
        object_id: str | None,
        occurred_after: datetime,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 100))
        if self.backend == "sqlite":
            sql = "SELECT * FROM evidence_events WHERE tenant_id=? AND source=? AND event_type=? AND occurred_at>=?"
            params: list[Any] = [tenant_id, source, event_type, occurred_after.isoformat()]
            if object_id is not None:
                sql += " AND object_id=?"
                params.append(object_id)
            sql += " ORDER BY occurred_at DESC LIMIT ?"
            params.append(bounded)
            with self._connect() as con:
                rows = con.execute(sql, params).fetchall()
            normalized = [dict(row) for row in rows]
        else:
            sql = "SELECT * FROM evidence_events WHERE tenant_id=%s AND source=%s AND event_type=%s AND occurred_at>=%s"
            params = [tenant_id, source, event_type, occurred_after.isoformat()]
            if object_id is not None:
                sql += " AND object_id=%s"
                params.append(object_id)
            sql += " ORDER BY occurred_at DESC LIMIT %s"
            params.append(bounded)
            with self._pg_connect() as con:
                with con.cursor() as cur:
                    cur.execute(sql, params)
                    normalized = cur.fetchall()
        return [
            {
                "event_id": row["event_id"],
                "source": row["source"],
                "event_type": row["event_type"],
                "object_id": row["object_id"],
                "occurred_at": row["occurred_at"],
                "payload": json.loads(row["payload_json"]),
                "payload_hash": row["payload_hash"],
            }
            for row in normalized
        ]

    def audit(
        self,
        tenant_id: str,
        action: str,
        object_type: str,
        object_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        body = json.dumps(metadata or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        values = (tenant_id, action, object_type, object_id, body, datetime.now(timezone.utc).isoformat())
        if self.backend == "sqlite":
            with self._connect() as con:
                con.execute(
                    "INSERT INTO audit_events(tenant_id,action,object_type,object_id,metadata_json,created_at) VALUES(?,?,?,?,?,?)",
                    values,
                )
            return
        with self._pg_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_events(tenant_id,action,object_type,object_id,metadata_json,created_at) VALUES(%s,%s,%s,%s,%s,%s)",
                    values,
                )

    def list_audit(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 500))
        if self.backend == "sqlite":
            with self._connect() as con:
                rows = con.execute(
                    "SELECT action,object_type,object_id,metadata_json,created_at FROM audit_events WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                    (tenant_id, bounded),
                ).fetchall()
            normalized = [
                {
                    "action": row[0],
                    "object_type": row[1],
                    "object_id": row[2],
                    "metadata_json": row[3],
                    "created_at": row[4],
                }
                for row in rows
            ]
        else:
            with self._pg_connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "SELECT action,object_type,object_id,metadata_json,created_at FROM audit_events WHERE tenant_id=%s ORDER BY id DESC LIMIT %s",
                        (tenant_id, bounded),
                    )
                    normalized = cur.fetchall()
        return [
            {
                "action": row["action"],
                "object_type": row["object_type"],
                "object_id": row["object_id"],
                "metadata": json.loads(row["metadata_json"]),
                "created_at": row["created_at"],
            }
            for row in normalized
        ]
