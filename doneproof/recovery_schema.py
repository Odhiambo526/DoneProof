"""Migration 4: additive recovery ledger; existing receipt bytes are never rewritten."""

SCHEMA = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_receipt_tenant_id ON receipts(tenant_id,receipt_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_tenant_id ON evidence_events(tenant_id,event_id)",
    """CREATE TABLE IF NOT EXISTS recovery_snapshots (
        tenant_id TEXT NOT NULL, root_id TEXT NOT NULL, contract_json TEXT NOT NULL,
        baselines_json TEXT NOT NULL, assurance_level TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY(tenant_id,root_id),
        FOREIGN KEY(tenant_id,root_id) REFERENCES receipts(tenant_id,receipt_id))""",
    """CREATE TABLE IF NOT EXISTS recovery_nodes (
        tenant_id TEXT NOT NULL, root_id TEXT NOT NULL, receipt_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal>=0), previous_receipt_id TEXT, receipt_hash TEXT NOT NULL,
        PRIMARY KEY(tenant_id,receipt_id), UNIQUE(tenant_id,root_id,ordinal),
        UNIQUE(tenant_id,previous_receipt_id), UNIQUE(tenant_id,root_id,receipt_id),
        FOREIGN KEY(tenant_id,root_id) REFERENCES recovery_snapshots(tenant_id,root_id),
        FOREIGN KEY(tenant_id,receipt_id) REFERENCES receipts(tenant_id,receipt_id),
        FOREIGN KEY(tenant_id,root_id,previous_receipt_id) REFERENCES recovery_nodes(tenant_id,root_id,receipt_id))""",
    """CREATE TABLE IF NOT EXISTS recovery_chains (
        tenant_id TEXT NOT NULL, root_id TEXT NOT NULL, head_id TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 20),
        max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 0 AND 20),
        automatic INTEGER NOT NULL DEFAULT 0 CHECK(automatic IN (0,1)), active_job_id TEXT,
        PRIMARY KEY(tenant_id,root_id),
        FOREIGN KEY(tenant_id,root_id) REFERENCES recovery_snapshots(tenant_id,root_id),
        FOREIGN KEY(tenant_id,root_id,head_id) REFERENCES recovery_nodes(tenant_id,root_id,receipt_id))""",
    """CREATE TABLE IF NOT EXISTS recovery_attempts (
        tenant_id TEXT NOT NULL, root_id TEXT NOT NULL, attempt INTEGER NOT NULL CHECK(attempt BETWEEN 1 AND 20),
        job_id TEXT NOT NULL, previous_receipt_id TEXT NOT NULL, event_id TEXT, created_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY(tenant_id,root_id,attempt), UNIQUE(tenant_id,job_id),
        FOREIGN KEY(tenant_id,root_id) REFERENCES recovery_snapshots(tenant_id,root_id),
        FOREIGN KEY(tenant_id,root_id,previous_receipt_id) REFERENCES recovery_nodes(tenant_id,root_id,receipt_id),
        FOREIGN KEY(tenant_id,job_id) REFERENCES verification_jobs(tenant_id,id))""",
    """CREATE TABLE IF NOT EXISTS recovery_watches (
        tenant_id TEXT NOT NULL, root_id TEXT NOT NULL, condition_id TEXT NOT NULL,
        source TEXT NOT NULL, event_type TEXT NOT NULL, object_id TEXT NOT NULL,
        PRIMARY KEY(tenant_id,root_id,condition_id),
        FOREIGN KEY(tenant_id,root_id) REFERENCES recovery_snapshots(tenant_id,root_id))""",
    "CREATE INDEX IF NOT EXISTS idx_recovery_watch ON recovery_watches(tenant_id,source,event_type,object_id)",
    """CREATE TABLE IF NOT EXISTS recovery_event_queue (
        tenant_id TEXT NOT NULL, root_id TEXT NOT NULL, event_id TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','DONE','IGNORED')),
        next_at DOUBLE PRECISION NOT NULL, reason TEXT, job_id TEXT,
        PRIMARY KEY(tenant_id,root_id,event_id),
        FOREIGN KEY(tenant_id,root_id) REFERENCES recovery_snapshots(tenant_id,root_id),
        FOREIGN KEY(tenant_id,event_id) REFERENCES evidence_events(tenant_id,event_id))""",
    "CREATE INDEX IF NOT EXISTS idx_recovery_event_pending ON recovery_event_queue(state,next_at)",
)


def migrate(con):
    for statement in SCHEMA:
        con.execute(statement)
    # Append-only audit facts are protected even from accidental application SQL updates.
    # Database administrators retain normal backup/retention authority.
    tables = ("receipts", "recovery_snapshots", "recovery_nodes", "recovery_attempts")
    import sqlite3
    if isinstance(con, sqlite3.Connection):
        for table in tables:
            for operation in ("UPDATE", "DELETE"):
                con.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()}
                    BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Immutable verification record'); END""")
    else:
        con.execute("""CREATE OR REPLACE FUNCTION doneproof_immutable_record() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Immutable verification record'; END; $$""")
        for table in tables:
            name = "immutable_" + table
            # Migration runs under the existing transaction-scoped advisory bootstrap lock.
            con.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
            con.execute(f"CREATE TRIGGER {name} BEFORE UPDATE OR DELETE ON {table} "
                        "FOR EACH ROW EXECUTE FUNCTION doneproof_immutable_record()")
    publication_guards(con, isinstance(con, sqlite3.Connection))


def publication_guards(con, sqlite):
    # A rolling deployment can leave an older worker on the shared queue. It
    # must never publish an unlinked recovery receipt or strand a terminal job.
    def field(path):
        return (f"CAST(json_extract(NEW.body_json,'$.{path}') AS TEXT)" if sqlite else
                "(NEW.body_json::jsonb #>> '{" + path.replace(".", ",") + "}')")
    comparisons = [("schema_version", "'1.1'"), ("previous_receipt_id", "a.previous_receipt_id"),
                   ("previous_receipt_hash", "p.receipt_hash"), ("recovery.chain_id", "a.root_id"),
                   ("recovery.attempt", "CAST(a.attempt AS TEXT)")]
    mismatch = " OR ".join(f"COALESCE({field(key)},'')<>{value}" for key, value in comparisons)
    invalid = f"""EXISTS(SELECT 1 FROM recovery_attempts a
        JOIN verification_jobs j ON j.tenant_id=a.tenant_id AND j.id=a.job_id
        JOIN receipts p ON p.tenant_id=a.tenant_id AND p.receipt_id=a.previous_receipt_id
        WHERE a.tenant_id=NEW.tenant_id AND j.receipt_id=NEW.receipt_id AND ({mismatch}))"""
    # Also cover ordinary jobs evaluated by a new worker but signed by an old
    # worker: its older model would discard 1.1 fields and make the receipt unreadable.
    incomplete = (f"{field('schema_version')}='1.1' AND ("
                  f"{field('recovery.chain_id')} IS NULL OR {field('recovery.attempt')} IS NULL "
                  f"OR {field('remediation')} IS NULL)")
    invalid = f"({invalid}) OR ({incomplete})"
    release = "UPDATE recovery_chains SET active_job_id=NULL WHERE tenant_id=NEW.tenant_id AND active_job_id=NEW.id;"
    terminal = "NEW.state IN ('COMPLETE','PARTIAL_FAILURE','EXPIRED','INTERNAL_ERROR')"
    if sqlite:
        con.execute(f"""CREATE TRIGGER IF NOT EXISTS recovery_publication_guard BEFORE INSERT ON receipts
            WHEN {invalid} BEGIN SELECT RAISE(ABORT,'Recovery publication requires linked receipt'); END""")
        con.execute(f"""CREATE TRIGGER IF NOT EXISTS recovery_release_terminal AFTER UPDATE OF state ON verification_jobs
            WHEN {terminal} BEGIN {release} END""")
    else:
        con.execute(f"""CREATE OR REPLACE FUNCTION doneproof_recovery_publication_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN IF {invalid} THEN
            RAISE EXCEPTION 'Recovery publication requires linked receipt'; END IF; RETURN NEW; END; $$""")
        con.execute(f"""CREATE OR REPLACE FUNCTION doneproof_recovery_release_terminal() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN {release} RETURN NEW; END; $$""")
        con.execute("DROP TRIGGER IF EXISTS recovery_publication_guard ON receipts")
        con.execute("""CREATE TRIGGER recovery_publication_guard BEFORE INSERT ON receipts
            FOR EACH ROW EXECUTE FUNCTION doneproof_recovery_publication_guard()""")
        con.execute("DROP TRIGGER IF EXISTS recovery_release_terminal ON verification_jobs")
        con.execute(f"""CREATE TRIGGER recovery_release_terminal AFTER UPDATE OF state ON verification_jobs
            FOR EACH ROW WHEN ({terminal}) EXECUTE FUNCTION doneproof_recovery_release_terminal()""")
