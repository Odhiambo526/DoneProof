from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from doneproof import store as store_module
from doneproof.client import DoneProofClient
from doneproof.domain import CompletionContract, VerificationReceipt
from doneproof.engine import VerificationEngine
from doneproof.recovery_store import RecoveryStore
from doneproof.signing import ReceiptSigner
from doneproof.store import Store
from doneproof.worker import VerificationWorker
from tests.test_jobs import run
from tests.test_recovery import StateProvider

LEGACY = json.loads((Path(__file__).parent / "fixtures/legacy_recovery_receipt.json").read_text())


def test_legacy_receipt_canonical_payload_and_signature_are_unchanged():
    raw = LEGACY["receipt"]
    receipt = VerificationReceipt.model_validate(raw)
    assert receipt.model_dump(mode="json") == raw
    assert "remediation" not in receipt.model_dump(mode="json")
    assert DoneProofClient.verify_receipt(raw, raw["public_key"])
    assert not DoneProofClient.verify_receipt({**raw, "previous_receipt_id": "unsigned-link"}, raw["public_key"])
    assert not DoneProofClient.verify_receipt({**raw, "remediation": [{"condition": "p0"}]}, raw["public_key"])
    receipt.previous_receipt_id = "unsigned-link"
    assert not ReceiptSigner.verify(receipt)


def test_older_worker_cannot_discard_new_receipt_fields_at_signing_handoff(connection_settings):
    store = Store(connection_settings.storage_dsn)
    db = RecoveryStore(store)
    # This is the shape an older model emits after loading a newer unsigned
    # checkpoint: schema_version survives but unknown recovery fields do not.
    raw = {**LEGACY["receipt"], "schema_version": "1.1"}
    with pytest.raises(Exception, match="Recovery publication requires linked receipt"):
        with db.transaction() as con:
            db.execute(con, """INSERT INTO receipts
                (tenant_id,receipt_id,contract_id,verdict,body_json,verified_at,receipt_hash,signature)
                VALUES(?,?,?,?,?,?,?,?)""", ("tenant-a", raw["receipt_id"], raw["contract_id"], raw["verdict"],
                    json.dumps(raw), raw["verified_at"], raw["receipt_hash"], raw["signature"]))


def test_additive_migration_preserves_production_bytes_and_enrolls_legacy_receipt(connection_settings, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "migrate_recovery", lambda con: None)
        patch.setattr(store_module, "migrate_providers", lambda con, **kwargs: None)
        legacy = Store(connection_settings.storage_dsn)
    contract = CompletionContract.model_validate(LEGACY["contract"])
    receipt = VerificationReceipt.model_validate(LEGACY["receipt"])
    legacy.save_contract("tenant-a", contract)
    legacy.save_receipt("tenant-a", receipt)
    legacy.save_idempotency("tenant-a", "old-retry", "old-request", receipt.receipt_id)
    db = RecoveryStore(legacy)
    with db.transaction() as con:
        exact = db.execute(con, "SELECT body_json FROM receipts WHERE receipt_id=?", (receipt.receipt_id,)).fetchone()["body_json"]
        if db.pg:
            con.execute("DELETE FROM schema_migrations WHERE version=4")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: Store(connection_settings.storage_dsn), range(4)))
    upgraded = Store(connection_settings.storage_dsn)
    assert upgraded.get_idempotency("tenant-a", "old-retry")["receipt_id"] == receipt.receipt_id
    with db.transaction() as con:
        assert db.execute(con, "SELECT body_json FROM receipts WHERE receipt_id=?", (receipt.receipt_id,)).fetchone()["body_json"] == exact
        if db.pg:
            assert con.execute("SELECT version FROM schema_migrations WHERE version=4").fetchone()
    engine = VerificationEngine({"github": StateProvider()}, ReceiptSigner(connection_settings))
    scheduled, _ = RecoveryStore(upgraded, 3).reverify("tenant-a", receipt.receipt_id, "new", "hash")
    result = run(VerificationWorker(upgraded, engine), scheduled["id"])
    child = upgraded.get_receipt("tenant-a", result["receipt_id"])
    assert child.previous_receipt_id == receipt.receipt_id and child.previous_receipt_hash == receipt.receipt_hash
    assert child.schema_version == "1.1" and ReceiptSigner.verify(child)
    assert upgraded.get_receipt("tenant-a", receipt.receipt_id).schema_version == "1.0"
    assert child.key_id != receipt.key_id  # key rotation does not rewrite the original receipt


@pytest.mark.parametrize("table", ["receipts", "recovery_nodes", "recovery_snapshots", "recovery_attempts"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_ledger_facts_are_immutable_at_database_layer(connection_settings, table, operation):
    store = Store(connection_settings.storage_dsn)
    contract = CompletionContract.model_validate(LEGACY["contract"])
    receipt = VerificationReceipt.model_validate(LEGACY["receipt"])
    store.save_contract("tenant-a", contract)
    store.save_receipt("tenant-a", receipt)
    db = RecoveryStore(store)
    db.reverify("tenant-a", receipt.receipt_id, "retry", "hash")
    sql = f"UPDATE {table} SET tenant_id=tenant_id" if operation == "UPDATE" else f"DELETE FROM {table}"
    with pytest.raises(Exception, match="Immutable verification record"):
        with db.transaction() as con:
            con.execute(sql)


def test_chain_foreign_keys_reject_cross_tenant_links(connection_settings):
    store = Store(connection_settings.storage_dsn)
    contract = CompletionContract.model_validate(LEGACY["contract"])
    receipt = VerificationReceipt.model_validate(LEGACY["receipt"])
    store.save_contract("tenant-a", contract)
    store.save_receipt("tenant-a", receipt)
    db = RecoveryStore(store)
    db.history("tenant-a", receipt.receipt_id)
    with pytest.raises(Exception):
        with db.transaction() as con:
            db.execute(con, """INSERT INTO recovery_chains(tenant_id,root_id,head_id,max_attempts)
                VALUES('tenant-b',?,?,5)""", (receipt.receipt_id, receipt.receipt_id))
