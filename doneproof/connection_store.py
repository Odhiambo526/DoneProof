"""Additive connection persistence. Every resource lookup includes the tenant."""
from __future__ import annotations

import json
import secrets
import time
from contextlib import contextmanager

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS connections (
        tenant_id TEXT NOT NULL, id TEXT NOT NULL, provider TEXT NOT NULL
            CHECK(provider IN ('gmail','github')),
        state TEXT NOT NULL CHECK(state IN
            ('connected','expired','reconnect_required','disabled','error')),
        account_id TEXT, account_label TEXT, credential_ciphertext TEXT,
        scopes_json TEXT NOT NULL DEFAULT '[]', expires_at BIGINT, refresh_expires_at BIGINT,
        revision BIGINT NOT NULL DEFAULT 0, authorization_version BIGINT NOT NULL DEFAULT 0,
        lease_id TEXT, lease_until BIGINT NOT NULL DEFAULT 0,
        last_checked_at BIGINT, error_code TEXT, revocation_pending INTEGER NOT NULL DEFAULT 0,
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
        PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,provider)
    )""",
    """CREATE TABLE IF NOT EXISTS connection_oauth_states (
        state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL, tenant_id TEXT NOT NULL,
        connection_id TEXT NOT NULL, provider TEXT NOT NULL,
        authorization_version BIGINT NOT NULL, verifier_ciphertext TEXT NOT NULL,
        redirect_uri TEXT NOT NULL, expires_at BIGINT NOT NULL,
        FOREIGN KEY(tenant_id,connection_id) REFERENCES connections(tenant_id,id)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_connection_oauth_expiry ON connection_oauth_states(expires_at)""",
    """CREATE TABLE IF NOT EXISTS connection_baseline_bindings (
        tenant_id TEXT NOT NULL, contract_id TEXT NOT NULL, condition_id TEXT NOT NULL,
        provider TEXT NOT NULL, identity TEXT NOT NULL,
        PRIMARY KEY(tenant_id,contract_id,condition_id,provider)
    )""",
    """CREATE TABLE IF NOT EXISTS connection_revocations (
        tenant_id TEXT NOT NULL, connection_id TEXT NOT NULL, id TEXT NOT NULL,
        credential_ciphertext TEXT NOT NULL,
        PRIMARY KEY(tenant_id,connection_id,id),
        FOREIGN KEY(tenant_id,connection_id) REFERENCES connections(tenant_id,id)
    )""",
)


def migrate(con):
    for statement in SCHEMA:
        con.execute(statement)


class ConnectionStore:
    def __init__(self, store):
        self.store = store
        self.pg = store.backend == "postgresql"

    @contextmanager
    def transaction(self):
        con = self.store._pg_connect() if self.pg else self.store._connect()
        try:
            with con:
                if not self.pg:
                    con.execute("BEGIN IMMEDIATE")
                yield con
        finally:
            con.close()

    def execute(self, con, sql, args=()):
        return con.execute(sql.replace("?", "%s") if self.pg else sql, args)

    def _row(self, cur):
        row = cur.fetchone()
        return dict(row) if row else None

    def get(self, tenant, *, provider=None, connection_id=None):
        with self.transaction() as con:
            if provider is not None:
                return self._row(self.execute(con,
                    "SELECT * FROM connections WHERE tenant_id=? AND provider=?", (tenant, provider)))
            return self._row(self.execute(con,
                "SELECT * FROM connections WHERE tenant_id=? AND id=?", (tenant, connection_id)))

    def list(self, tenant):
        with self.transaction() as con:
            return [dict(r) for r in self.execute(con,
                "SELECT * FROM connections WHERE tenant_id=? ORDER BY provider", (tenant,)).fetchall()]

    def ensure(self, tenant, provider):
        now = int(time.time())
        with self.transaction() as con:
            self.execute(con, """INSERT INTO connections
                (tenant_id,id,provider,state,created_at,updated_at) VALUES(?,?,?,'reconnect_required',?,?)
                ON CONFLICT(tenant_id,provider) DO NOTHING""",
                (tenant, "cn_" + secrets.token_hex(16), provider, now, now))
        return self.get(tenant, provider=provider)

    def update(self, row, **fields):
        allowed = {
            "state", "account_id", "account_label", "credential_ciphertext", "scopes_json",
            "expires_at", "refresh_expires_at", "last_checked_at", "error_code", "revocation_pending",
        }
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid connection update")
        fields["updated_at"] = int(time.time())
        assignments = ",".join(f"{key}=?" for key in fields)
        with self.transaction() as con:
            return self._row(self.execute(con, f"""UPDATE connections SET {assignments},
                revision=revision+1,lease_id=NULL,lease_until=0
                WHERE tenant_id=? AND id=? AND revision=? RETURNING *""",
                (*fields.values(), row["tenant_id"], row["id"], row["revision"])))

    def start_oauth(self, row, state_hash, browser_hash, verifier, redirect_uri):
        now = int(time.time())
        with self.transaction() as con:
            self.execute(con, "DELETE FROM connection_oauth_states WHERE expires_at<?", (now,))
            updated = self._row(self.execute(con, """UPDATE connections
                SET authorization_version=authorization_version+1,revision=revision+1,
                    lease_id=NULL,lease_until=0,updated_at=?
                WHERE tenant_id=? AND id=? AND revision=? AND revocation_pending=0 RETURNING *""",
                (now, row["tenant_id"], row["id"], row["revision"])))
            if not updated:
                return None
            self.execute(con, "DELETE FROM connection_oauth_states WHERE tenant_id=? AND connection_id=?",
                (row["tenant_id"], row["id"]))
            self.execute(con, """INSERT INTO connection_oauth_states
                (state_hash,browser_hash,tenant_id,connection_id,provider,authorization_version,
                 verifier_ciphertext,redirect_uri,expires_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (state_hash, browser_hash, row["tenant_id"], row["id"], row["provider"],
                 updated["authorization_version"], verifier, redirect_uri, now + 600))
            return updated

    def consume_oauth(self, provider, state_hash, browser_hash):
        with self.transaction() as con:
            return self._row(self.execute(con, """DELETE FROM connection_oauth_states
                WHERE provider=? AND state_hash=? AND browser_hash=? AND expires_at>=? RETURNING *""",
                (provider, state_hash, browser_hash, int(time.time()))))

    def acquire_lease(self, row):
        lease = secrets.token_hex(16)
        now = int(time.time())
        with self.transaction() as con:
            return self._row(self.execute(con, """UPDATE connections SET lease_id=?,lease_until=?
                WHERE tenant_id=? AND id=? AND revision=? AND lease_until<=? AND state<>'disabled'
                RETURNING *""", (lease, now + 120, row["tenant_id"], row["id"], row["revision"], now)))

    def disable(self, row):
        with self.transaction() as con:
            updated = self._row(self.execute(con, """UPDATE connections SET state='disabled',
                revision=revision+1,authorization_version=authorization_version+1,
                lease_id=NULL,lease_until=0,updated_at=?,
                revocation_pending=CASE WHEN credential_ciphertext IS NULL AND NOT EXISTS
                    (SELECT 1 FROM connection_revocations r WHERE r.tenant_id=connections.tenant_id
                     AND r.connection_id=connections.id) THEN 0 ELSE 1 END
                WHERE tenant_id=? AND id=? RETURNING *""",
                (int(time.time()), row["tenant_id"], row["id"])))
            self.execute(con, "DELETE FROM connection_oauth_states WHERE tenant_id=? AND connection_id=?",
                (row["tenant_id"], row["id"]))
            return updated

    def bind(self, tenant, contract_id, condition_id, provider, identity, capture):
        with self.transaction() as con:
            if capture:
                self.execute(con, """INSERT INTO connection_baseline_bindings
                    (tenant_id,contract_id,condition_id,provider,identity) VALUES(?,?,?,?,?)
                    ON CONFLICT(tenant_id,contract_id,condition_id,provider) DO NOTHING""",
                    (tenant, contract_id, condition_id, provider, identity))
            row = self._row(self.execute(con, """SELECT identity FROM connection_baseline_bindings
                WHERE tenant_id=? AND contract_id=? AND condition_id=? AND provider=?""",
                (tenant, contract_id, condition_id, provider)))
            return bool(row and row["identity"] == identity)

    def queue_revocation(self, row, encrypted):
        with self.transaction() as con:
            self.execute(con, """INSERT INTO connection_revocations
                (tenant_id,connection_id,id,credential_ciphertext) VALUES(?,?,?,?)""",
                (row["tenant_id"], row["id"], secrets.token_hex(16), encrypted))
            self.execute(con, """UPDATE connections SET state='disabled',revocation_pending=1,
                authorization_version=authorization_version+1,revision=revision+1,
                lease_id=NULL,lease_until=0,error_code='revocation_pending'
                WHERE tenant_id=? AND id=?""", (row["tenant_id"], row["id"]))
            self.execute(con, "DELETE FROM connection_oauth_states WHERE tenant_id=? AND connection_id=?",
                         (row["tenant_id"], row["id"]))

    def revocations(self, row):
        with self.transaction() as con:
            return [dict(r) for r in self.execute(con, """SELECT * FROM connection_revocations
                WHERE tenant_id=? AND connection_id=?""", (row["tenant_id"], row["id"])).fetchall()]

    def remove_revocation(self, row, revocation_id):
        with self.transaction() as con:
            self.execute(con, "DELETE FROM connection_revocations WHERE tenant_id=? AND connection_id=? AND id=?",
                         (row["tenant_id"], row["id"], revocation_id))

    def confirm_external_revocation(self, row):
        with self.transaction() as con:
            updated = self._row(self.execute(con, """UPDATE connections SET credential_ciphertext=NULL,
                revocation_pending=0,error_code=NULL,expires_at=NULL,refresh_expires_at=NULL,
                revision=revision+1,updated_at=? WHERE tenant_id=? AND id=? AND revision=?
                AND state='disabled' RETURNING *""",
                (int(time.time()), row["tenant_id"], row["id"], row["revision"])))
            if updated:
                self.execute(con, "DELETE FROM connection_revocations WHERE tenant_id=? AND connection_id=?",
                             (row["tenant_id"], row["id"]))
            return updated

    def audit(self, row, action):
        # Explicit projection: never include provider payloads, errors, state, codes or credentials.
        self.store.audit(row["tenant_id"], "connection." + action, "connection", row["id"],
            {"provider": row["provider"], "connection_status": row["state"]})

    @staticmethod
    def public(row):
        keys = ("id", "provider", "state", "account_label", "expires_at", "refresh_expires_at",
                "last_checked_at", "error_code", "created_at", "updated_at")
        result = {k: row[k] for k in keys}
        result["scopes"] = json.loads(row["scopes_json"])
        result["revocation_pending"] = bool(row["revocation_pending"])
        if result["state"] == "connected" and row["expires_at"] and row["expires_at"] <= time.time():
            result["state"] = "expired"
        return result
