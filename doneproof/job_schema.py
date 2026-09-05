"""Migration 3. Additive DDL, executed under the existing migration transaction/lock."""
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS verification_tenants (
        tenant_id TEXT PRIMARY KEY
    )""",
    """CREATE TABLE IF NOT EXISTS verification_jobs (
        tenant_id TEXT NOT NULL, id TEXT NOT NULL,
        idempotency_hash TEXT NOT NULL, request_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('QUEUED','OBSERVING','EVALUATING','SIGNING',
            'COMPLETE','PARTIAL_FAILURE','EXPIRED','INTERNAL_ERROR')),
        revision BIGINT NOT NULL DEFAULT 0,
        contract_json TEXT NOT NULL, baselines_json TEXT NOT NULL,
        assurance_level TEXT NOT NULL CHECK(assurance_level IN ('submitted','registered')),
        condition_count INTEGER NOT NULL CHECK(condition_count BETWEEN 1 AND 1000),
        created_at DOUBLE PRECISION NOT NULL, started_at DOUBLE PRECISION,
        finished_at DOUBLE PRECISION, deadline_at DOUBLE PRECISION NOT NULL,
        next_run_at DOUBLE PRECISION NOT NULL,
        lease_token TEXT, lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
        terminal_reason TEXT, receipt_id TEXT NOT NULL UNIQUE,
        unsigned_receipt_json TEXT, signer_key_id TEXT,
        callback_id TEXT, callback_fingerprint TEXT,
        PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,idempotency_hash)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_verification_jobs_ready
        ON verification_jobs(next_run_at,lease_until) WHERE finished_at IS NULL""",
    """CREATE INDEX IF NOT EXISTS idx_verification_jobs_tenant
        ON verification_jobs(tenant_id,created_at DESC)""",
    """CREATE TABLE IF NOT EXISTS verification_conditions (
        tenant_id TEXT NOT NULL, job_id TEXT NOT NULL, condition_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL, provider TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('PENDING','RUNNING','OBSERVED','EVALUATED','ABORTED')),
        attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT,
        observation_json TEXT, result_json TEXT,
        infrastructure_failure INTEGER NOT NULL DEFAULT 0,
        error_code TEXT, next_attempt_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY(tenant_id,job_id,condition_id),
        FOREIGN KEY(tenant_id,job_id) REFERENCES verification_jobs(tenant_id,id)
    )""",
    """CREATE TABLE IF NOT EXISTS verification_attempts (
        tenant_id TEXT NOT NULL, job_id TEXT NOT NULL, condition_id TEXT NOT NULL,
        attempt INTEGER NOT NULL, started_at DOUBLE PRECISION NOT NULL,
        finished_at DOUBLE PRECISION, outcome TEXT, error_code TEXT,
        next_attempt_at DOUBLE PRECISION,
        PRIMARY KEY(tenant_id,job_id,condition_id,attempt),
        FOREIGN KEY(tenant_id,job_id,condition_id)
            REFERENCES verification_conditions(tenant_id,job_id,condition_id)
    )""",
    """CREATE TABLE IF NOT EXISTS verification_provider_slots (
        provider TEXT NOT NULL, slot INTEGER NOT NULL,
        lease_token TEXT, lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
        PRIMARY KEY(provider,slot)
    )""",
    """CREATE TABLE IF NOT EXISTS verification_callback_outbox (
        tenant_id TEXT NOT NULL, job_id TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE,
        callback_id TEXT NOT NULL, callback_fingerprint TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('PENDING','SENDING','DELIVERED','DEAD')),
        attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT,
        lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
        next_attempt_at DOUBLE PRECISION NOT NULL, deadline_at DOUBLE PRECISION NOT NULL,
        error_code TEXT,
        PRIMARY KEY(tenant_id,job_id),
        FOREIGN KEY(tenant_id,job_id) REFERENCES verification_jobs(tenant_id,id)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_verification_callbacks_ready
        ON verification_callback_outbox(next_attempt_at,lease_until) WHERE state IN ('PENDING','SENDING')""",
)


def migrate(con):
    for statement in SCHEMA:
        con.execute(statement)
