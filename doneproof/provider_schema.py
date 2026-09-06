"""Migration 5: extensible connection IDs and durable provider manifests.

Runs inside Store's existing migration transaction and startup lock. Credentials,
tenant keys, OAuth state, revocations, baselines and signed receipts retain their IDs.
"""
from .connection_store import SCHEMA


def migrate(con, *, pg):
    con.execute("""CREATE TABLE IF NOT EXISTS provider_contract_bindings (
        tenant_id TEXT NOT NULL, contract_id TEXT NOT NULL, provider TEXT NOT NULL,
        fingerprint TEXT NOT NULL, PRIMARY KEY(tenant_id,contract_id,provider),
        FOREIGN KEY(tenant_id,contract_id) REFERENCES contracts(tenant_id,id)
    )""")
    if pg:
        con.execute("ALTER TABLE connections DROP CONSTRAINT IF EXISTS connections_provider_check")
        con.execute("ALTER TABLE verification_jobs ADD COLUMN IF NOT EXISTS provider_manifest_json TEXT NOT NULL DEFAULT '{}'")
    else:
        schema = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='connections'").fetchone()[0]
        if "CHECK(provider IN ('gmail','github'))" in schema:
            # Keep foreign_keys enabled. Temporarily retain dependent rows in
            # transaction-local tables while replacing the constrained parent.
            children = ("connection_oauth_states", "connection_revocations")
            for table in children:
                con.execute(f"CREATE TEMP TABLE {table}_migration5 AS SELECT * FROM {table}")
                con.execute(f"DELETE FROM {table}")
            definition = SCHEMA[0].replace("IF NOT EXISTS connections", "connections_migration5")
            definition = definition.replace("CHECK(provider IN ('gmail','github'))", "CHECK(length(provider) BETWEEN 1 AND 64)")
            con.execute(definition)
            con.execute("INSERT INTO connections_migration5 SELECT * FROM connections")
            con.execute("DROP TABLE connections")
            con.execute("ALTER TABLE connections_migration5 RENAME TO connections")
            for table in children:
                con.execute(f"INSERT INTO {table} SELECT * FROM {table}_migration5")
                con.execute(f"DROP TABLE {table}_migration5")
            if con.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("Provider migration failed foreign key validation")
        columns = {row[1] for row in con.execute("PRAGMA table_info(verification_jobs)")}
        if "provider_manifest_json" not in columns:
            con.execute("ALTER TABLE verification_jobs ADD COLUMN provider_manifest_json TEXT NOT NULL DEFAULT '{}'")


def synchronize_slots(con, registry, *, pg):
    placeholder = "%s" if pg else "?"
    for provider, count in registry.concurrency().items():
        for slot in range(count):
            con.execute(f"INSERT INTO verification_provider_slots(provider,slot) VALUES({placeholder},{placeholder}) "
                        "ON CONFLICT(provider,slot) DO NOTHING", (provider, slot))
    # Existing leased slots are never deleted. Workers filter by their declared
    # limit, and a job's fingerprint prevents execution under changed policies.
