"""Schema 7: preparation reservations and session/job associations only.

Verification jobs and signed receipt payloads retain their existing schemas.
"""


def migrate(con):
    con.execute("""CREATE TABLE IF NOT EXISTS connection_operations (
        tenant_id TEXT NOT NULL, idempotency_hash TEXT NOT NULL,
        connection_id TEXT NOT NULL, expected_revision BIGINT NOT NULL,
        PRIMARY KEY(tenant_id,idempotency_hash),
        FOREIGN KEY(tenant_id,connection_id) REFERENCES connections(tenant_id,id)
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS assurance_sessions (
        tenant_id TEXT NOT NULL, id TEXT NOT NULL, idempotency_hash TEXT NOT NULL,
        request_hash TEXT NOT NULL, task TEXT NOT NULL,
        preparation_state TEXT NOT NULL CHECK(preparation_state IN
            ('PREPARING','NEEDS_CLARIFICATION','READY_FOR_EXECUTION','PREPARATION_FAILED')),
        compiler_json TEXT, contract_id TEXT, providers_json TEXT NOT NULL DEFAULT '[]',
        first_job_id TEXT, verification_hash TEXT,
        created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
        preparation_deadline DOUBLE PRECISION NOT NULL,
        PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,idempotency_hash),
        FOREIGN KEY(tenant_id,contract_id) REFERENCES contracts(tenant_id,id),
        FOREIGN KEY(tenant_id,first_job_id) REFERENCES verification_jobs(tenant_id,id)
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS assurance_requests (
        tenant_id TEXT NOT NULL, session_id TEXT NOT NULL, idempotency_hash TEXT NOT NULL,
        request_hash TEXT NOT NULL, job_id TEXT NOT NULL,
        PRIMARY KEY(tenant_id,session_id,idempotency_hash),
        FOREIGN KEY(tenant_id,session_id) REFERENCES assurance_sessions(tenant_id,id),
        FOREIGN KEY(tenant_id,job_id) REFERENCES verification_jobs(tenant_id,id)
    )""")
