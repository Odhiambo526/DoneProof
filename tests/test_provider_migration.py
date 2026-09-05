import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from doneproof import store as store_module
from doneproof.connection_store import ConnectionStore
from doneproof.connections import ConnectionService
from doneproof.domain import CompletionContract, VerificationReceipt
from doneproof.store import Store
from tests.connection_helpers import seed
from tests.sdk_provider import registry
from tests.test_recovery_migration import LEGACY

TABLES = ("connections", "connection_oauth_states", "connection_baseline_bindings", "connection_revocations",
          "contracts", "receipts", "idempotency", "audit_events")


def snapshot(db):
    with db.transaction() as con:
        return {table: [dict(row) for row in db.execute(con, f"SELECT * FROM {table}").fetchall()] for table in TABLES}


@pytest.fixture
def previous_schema(connection_settings, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "migrate_providers", lambda con, **kwargs: None)
        store = Store(connection_settings.storage_dsn)
    service = ConnectionService(store, connection_settings)
    gmail = seed(service)
    seed(service, "github", "tenant-b")
    service.start("tenant-a", "gmail")
    service.db.bind("tenant-a", "old-contract", "p0", "gmail", "old-account-binding", True)
    service.db.queue_revocation(gmail, gmail["credential_ciphertext"])
    contract = CompletionContract.model_validate(LEGACY["contract"])
    receipt = VerificationReceipt.model_validate(LEGACY["receipt"])
    store.save_contract("tenant-a", contract)
    store.save_receipt("tenant-a", receipt)
    store.save_idempotency("tenant-a", "legacy-key", "legacy-hash", receipt.receipt_id)
    store.audit("tenant-a", "legacy.event", "contract", contract.id, {"preserved": True})
    with service.db.transaction() as con:
        if service.db.pg:
            con.execute("DELETE FROM schema_migrations WHERE version=5")
        # Insert the exact old job columns: there was no provider manifest field.
        service.db.execute(con, """INSERT INTO verification_jobs
            (tenant_id,id,idempotency_hash,request_hash,state,contract_json,baselines_json,assurance_level,
             condition_count,created_at,deadline_at,next_run_at,receipt_id)
            VALUES('tenant-a','vj_old','old-idem','old-hash','QUEUED',?,'{}','submitted',?,?,?,?,'vr_old')""",
            (contract.model_dump_json(), len(contract.postconditions), time.time(), time.time()+300, time.time()))
    return store, service, snapshot(service.db)


def test_migration_preserves_encrypted_and_signed_bytes_with_concurrent_starts(previous_schema, connection_settings):
    store, service, before = previous_schema
    catalog = registry()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: Store(connection_settings.storage_dsn, catalog), range(4)))
    upgraded = Store(connection_settings.storage_dsn, catalog)
    db = ConnectionStore(upgraded)
    assert snapshot(db) == before
    original = before["connections"][0]
    assert service.vault.decrypt(db.get(original["tenant_id"], connection_id=original["id"]))
    assert upgraded.get_receipt("tenant-b", LEGACY["receipt"]["receipt_id"]) is None
    connection = db.ensure("tenant-a", "inventory")
    assert connection["provider"] == "inventory" and db.get("tenant-b", connection_id=connection["id"]) is None
    with db.transaction() as con:
        job = db.execute(con, "SELECT * FROM verification_jobs WHERE id='vj_old'").fetchone()
        assert json.loads(job["provider_manifest_json"]) == {}
        slots = db.execute(con, "SELECT COUNT(*) AS n FROM verification_provider_slots WHERE provider='inventory'").fetchone()
        assert slots["n"] == 2
        if db.pg:
            assert con.execute("SELECT version FROM schema_migrations WHERE version=5").fetchone()
        else:
            assert not con.execute("PRAGMA foreign_key_check").fetchall()
    # Composite foreign keys continue rejecting cross-tenant token revocations.
    with pytest.raises(Exception):
        with db.transaction() as con:
            db.execute(con, """INSERT INTO connection_revocations(tenant_id,connection_id,id,credential_ciphertext)
                VALUES('tenant-b',?,'invalid','ciphertext')""", (connection["id"],))


def test_migration_failure_rolls_back_all_schema_and_data_changes(previous_schema, connection_settings, monkeypatch):
    _, service, before = previous_schema
    def unavailable(*args, **kwargs):
        raise RuntimeError("Injected migration failure")
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "synchronize_slots", unavailable)
        with pytest.raises(RuntimeError, match="Injected"):
            Store(connection_settings.storage_dsn, registry())
    assert snapshot(service.db) == before
    with service.db.transaction() as con:
        if service.db.pg:
            columns = [r["column_name"] for r in con.execute("""SELECT column_name FROM information_schema.columns
                WHERE table_schema=current_schema() AND table_name='verification_jobs'""")]
        else:
            columns = [r[1] for r in con.execute("PRAGMA table_info(verification_jobs)")]
        assert "provider_manifest_json" not in columns
    Store(connection_settings.storage_dsn, registry())
