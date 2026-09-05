from __future__ import annotations

import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from doneproof.connection_crypto import CredentialVault
from doneproof.connection_store import ConnectionStore
from doneproof.connections import ConnectionService, digest
from doneproof.domain import CompletionContract
from doneproof.store import Store
from tests.connection_helpers import seed


def test_ciphertext_authenticates_tenant_provider_row_and_purpose(connection_settings):
    settings = connection_settings
    vault = CredentialVault(settings.connection_encryption_keys, settings.connection_active_key)
    row = {"tenant_id": "tenant-a", "provider": "gmail", "id": "connection"}
    envelope = vault.encrypt(row, {"access_token": "test-secret"})
    assert "test-secret" not in envelope
    assert vault.encrypt(row, {"access_token": "test-secret"}) != envelope
    assert vault.decrypt(row, envelope)["access_token"] == "test-secret"
    for changed in ({**row, "tenant_id": "tenant-b"}, {**row, "provider": "github"}, {**row, "id": "other"}):
        with pytest.raises(RuntimeError, match="unavailable"):
            vault.decrypt(changed, envelope)
    with pytest.raises(RuntimeError):
        vault.decrypt(row, envelope, purpose="oauth")
    value = json.loads(envelope)
    raw = bytearray(base64.b64decode(value["data"]))
    raw[-1] ^= 1
    value["data"] = base64.b64encode(raw).decode()
    with pytest.raises(RuntimeError):
        vault.decrypt(row, json.dumps(value))


def test_key_rotation_preserves_access_with_old_key_until_reencrypted(connection_settings):
    service = ConnectionService(Store(connection_settings.storage_dsn), connection_settings)
    row = seed(service)
    new_keys = {**connection_settings.connection_encryption_keys, "next": base64.b64encode(b"N" * 32).decode()}
    newer = replace(connection_settings, connection_encryption_keys=new_keys, connection_active_key="next")
    vault = CredentialVault(newer.connection_encryption_keys, newer.connection_active_key)
    new_envelope = vault.encrypt(row, vault.decrypt(row))
    assert json.loads(new_envelope)["kid"] == "next"
    next_only = CredentialVault({"next": new_keys["next"]}, "next")
    assert next_only.decrypt(row, new_envelope)
    with pytest.raises(RuntimeError):
        next_only.decrypt(row)


def test_oauth_state_single_use_expiry_browser_and_generation(connection_settings):
    service = ConnectionService(Store(connection_settings.storage_dsn), connection_settings)
    db = service.db
    row = db.ensure("tenant-a", "gmail")
    verifier = service.vault.encrypt(row, {"verifier": "sensitive-verifier"}, "oauth")
    row = db.start_oauth(row, digest("state"), digest("browser"), verifier, service.redirect_uri("gmail"))
    assert not db.consume_oauth("github", digest("state"), digest("browser"))
    assert not db.consume_oauth("gmail", digest("state"), digest("other-browser"))
    consumed = db.consume_oauth("gmail", digest("state"), digest("browser"))
    assert consumed and "sensitive-verifier" not in consumed["verifier_ciphertext"]
    assert not db.consume_oauth("gmail", digest("state"), digest("browser"))
    row = db.start_oauth(row, digest("state2"), digest("browser"), verifier, service.redirect_uri("gmail"))
    with db.transaction() as con:
        db.execute(con, "UPDATE connection_oauth_states SET expires_at=? WHERE state_hash=?",
                   (int(time.time()) - 1, digest("state2")))
    assert not db.consume_oauth("gmail", digest("state2"), digest("browser"))
    assert db.disable(row)["authorization_version"] > row["authorization_version"]


def test_database_races_use_compare_and_swap(connection_settings):
    service = ConnectionService(Store(connection_settings.storage_dsn), connection_settings)
    row = seed(service)
    db = service.db
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: db.acquire_lease(row), range(2)))
    assert sum(bool(r) for r in results) == 1
    disabled = db.disable(row)
    assert disabled["state"] == "disabled"
    assert db.update(row, state="connected") is None
    assert db.get("tenant-b", connection_id=row["id"]) is None
    assert not db.acquire_lease(disabled)


def test_baseline_bindings_are_tenant_condition_and_provider_scoped(connection_settings):
    db = ConnectionStore(Store(connection_settings.storage_dsn))
    assert db.bind("a", "run", "condition", "gmail", "account-a", True)
    assert db.bind("a", "run", "condition", "gmail", "account-a", False)
    assert not db.bind("a", "run", "condition", "gmail", "account-b", True)
    for tenant, condition, provider in (("b", "condition", "gmail"), ("a", "other", "gmail"), ("a", "condition", "github")):
        assert not db.bind(tenant, "run", condition, provider, "account-a", False)


def test_migration_is_additive_idempotent_and_preserves_signed_data(connection_settings, monkeypatch):
    # First initialize exactly the previous schema, then apply the new migration.
    monkeypatch.setattr("doneproof.store.migrate_connections", lambda con: None)
    original = Store(connection_settings.storage_dsn)
    contract = CompletionContract.model_validate({"task": "Existing production run", "postconditions": [{
        "id": "p1", "description": "existing condition", "provider": "github",
        "selector": {"repo": "example/project", "kind": "issue", "number": 1},
        "predicate": {"op": "eq", "path": "state", "expected": "closed"}}]})
    original.save_contract("tenant-a", contract)
    original.audit("tenant-a", "legacy.audit", "contract", contract.id, {"safe": True})
    with (original._pg_connect() if original.backend == "postgresql" else original._connect()) as con:
        if original.backend == "postgresql":
            con.execute("DELETE FROM schema_migrations WHERE version=2")
        # Exact bytes intentionally do not need a current model: migration must not rewrite receipts.
        query = """INSERT INTO receipts(receipt_id,contract_id,verdict,body_json,verified_at,receipt_hash,signature,tenant_id)
                   VALUES(?,?,?,?,?,?,?,?)"""
        con.execute(query.replace("?", "%s") if original.backend == "postgresql" else query,
                    ("legacy-receipt", contract.id, "VERIFIED", '{"legacy":"signed-bytes"}',
                     "2026-01-01T00:00:00+00:00", "old-hash", "old-signature", "tenant-a"))
    monkeypatch.undo()
    upgraded = Store(connection_settings.storage_dsn)
    restarted = Store(connection_settings.storage_dsn)
    assert restarted.get_contract("tenant-a", contract.id).task == contract.task
    assert restarted.list_audit("tenant-a", 10)[0]["action"] == "legacy.audit"
    db = ConnectionStore(upgraded)
    assert db.ensure("tenant-a", "gmail")["state"] == "reconnect_required"
    with db.transaction() as con:
        receipt = db._row(db.execute(con, "SELECT * FROM receipts WHERE tenant_id=? AND receipt_id=?",
                                    ("tenant-a", "legacy-receipt")))
        assert receipt["body_json"] == '{"legacy":"signed-bytes"}'
        assert receipt["signature"] == "old-signature" and receipt["receipt_hash"] == "old-hash"
        if db.pg:
            assert db._row(db.execute(con, "SELECT version FROM schema_migrations WHERE version=2"))


def test_concurrent_migration_cold_starts(connection_settings):
    with ThreadPoolExecutor(max_workers=3) as pool:
        stores = list(pool.map(lambda _: Store(connection_settings.storage_dsn), range(3)))
    assert all(store.ping() for store in stores)
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(lambda _: ConnectionStore(stores[0]).ensure("a", "gmail"), range(3)))
    assert len({row["id"] for row in rows}) == 1


def test_no_plaintext_credentials_in_database_or_audit(connection_settings):
    service = ConnectionService(Store(connection_settings.storage_dsn), connection_settings)
    seed(service)
    service.start("tenant-a", "gmail")
    with service.db.transaction() as con:
        for table in ("connections", "connection_oauth_states", "audit_events"):
            values = [dict(row) for row in service.db.execute(con, "SELECT * FROM " + table).fetchall()]
            assert "test-access-sentinel" not in json.dumps(values)
            assert "test-refresh-sentinel" not in json.dumps(values)
