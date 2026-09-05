from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from doneproof import store as store_module
from doneproof.domain import CompletionContract
from doneproof.engine import VerificationEngine
from doneproof.job_store import JobStore
from doneproof.signing import ReceiptSigner
from doneproof.store import Store
from tests.test_jobs import Provider, payload


def test_phase1_data_survives_additive_job_migration(connection_settings, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "migrate_jobs", lambda con: None)
        patch.setattr(store_module, "migrate_recovery", lambda con: None)
        patch.setattr(store_module, "migrate_providers", lambda con, **kwargs: None)
        patch.setattr(store_module, "synchronize_slots", lambda con, registry, **kwargs: None)
        legacy = Store(connection_settings.storage_dsn)
    db = JobStore(legacy)
    if db.pg:
        with db.transaction() as con:
            con.execute("DELETE FROM schema_migrations WHERE version=3")
    contract = CompletionContract.model_validate(payload()["contract"])
    legacy.save_contract("tenant-a", contract)
    receipt = asyncio.run(VerificationEngine({"github": Provider()}, ReceiptSigner(connection_settings)).verify(contract))
    legacy.save_receipt("tenant-a", receipt)
    legacy.save_idempotency("tenant-a", "old-key", "old-hash", receipt.receipt_id)
    with db.transaction() as con:
        exact = db.execute(con, "SELECT body_json FROM receipts WHERE receipt_id=?", (receipt.receipt_id,)).fetchone()["body_json"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: Store(connection_settings.storage_dsn), range(4)))
    upgraded = Store(connection_settings.storage_dsn)
    assert upgraded.get_contract("tenant-a", contract.id) == contract
    assert upgraded.get_idempotency("tenant-a", "old-key")["receipt_id"] == receipt.receipt_id
    with db.transaction() as con:
        assert db.execute(con, "SELECT body_json FROM receipts WHERE receipt_id=?", (receipt.receipt_id,)).fetchone()["body_json"] == exact
        assert db.execute(con, "SELECT COUNT(*) AS n FROM verification_provider_slots").fetchone()["n"] == 44
    # Composite foreign keys prevent accidentally attaching records to a different tenant's job.
    job, _ = db.create("tenant-a", "migrated", "hash", contract, {}, "submitted", 300)
    with pytest.raises(Exception):
        with db.transaction() as con:
            db.execute(con, """INSERT INTO verification_conditions
                (tenant_id,job_id,condition_id,ordinal,provider,state,next_attempt_at)
                VALUES('tenant-b',?,'foreign',0,'github','PENDING',0)""", (job["id"],))
    assert upgraded.get_receipt("tenant-b", receipt.receipt_id) is None
