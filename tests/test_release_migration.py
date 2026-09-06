"""Real schema-1 Store code plus populated signed fixtures across interrupted upgrades."""
import importlib.util
import json
from pathlib import Path

import pytest

from doneproof import store as store_module
from doneproof.connection_store import ConnectionStore
from doneproof.domain import CompletionContract, VerificationReceipt
from doneproof.signing import ReceiptSigner
from doneproof.store import Store

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ("migrate_connections", "migrate_jobs", "migrate_recovery", "migrate_providers",
              "migrate_browser_artifacts", "migrate_assurance")


def legacy_class():
    spec = importlib.util.spec_from_file_location(
        "doneproof._rc_schema1_store", ROOT / "tests/fixtures/production_store_v1.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Store


def snapshot(db):
    with db.transaction() as con:
        return [dict(row) for row in db.execute(con, "SELECT * FROM receipts ORDER BY receipt_id")]


@pytest.mark.parametrize("interruption", [None, *MIGRATIONS])
@pytest.mark.parametrize("schema", ["1.0", "1.1", "1.2"])
def test_populated_schema1_upgrade_and_transactional_interruption(connection_settings, monkeypatch, interruption, schema):
    legacy = legacy_class()(connection_settings.storage_dsn)
    contract = CompletionContract(task="Historical fixture", postconditions=[{
        "id": "p1", "description": "Historical outcome", "provider": "github", "selector": {"repo": "acme/api", "kind": "issue", "number": 1},
        "predicate": {"op": "eq", "path": "state", "expected": "closed"}}])
    legacy.save_contract("tenant-a", contract)
    fixtures = json.loads((ROOT / "sdk/typescript/test/fixtures.json").read_text())["receipts"]
    fixtures = [item for item in fixtures if item["receipt"]["schema_version"] == schema]
    for item in fixtures:
        legacy.save_receipt("tenant-a", VerificationReceipt.model_validate(item["receipt"]))
    legacy.audit("tenant-a", "fixture.registered", "contract", contract.id, {"fixture": True})
    legacy.save_idempotency("tenant-a", "historical-key", "historical-hash", fixtures[0]["receipt"]["receipt_id"])
    db = ConnectionStore(legacy)
    before = snapshot(db)
    if interruption:
        original = getattr(store_module, interruption)
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("Simulated migration process interruption")
        with monkeypatch.context() as patch:
            patch.setattr(store_module, interruption, interrupted)
            with pytest.raises(RuntimeError, match="interruption"):
                Store(connection_settings.storage_dsn)
        assert snapshot(db) == before
        with db.transaction() as con:
            if db.pg:
                assert [r["version"] for r in con.execute("SELECT version FROM schema_migrations")] == [1]
            else:
                assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='connections'").fetchone()
    current = Store(connection_settings.storage_dsn)
    assert snapshot(db) == before
    assert current.get_contract("tenant-a", contract.id) == contract
    assert current.get_contract("tenant-b", contract.id) is None
    assert current.get_idempotency("tenant-a", "historical-key")["request_hash"] == "historical-hash"
    assert current.list_audit("tenant-a")[0]["action"] == "fixture.registered"
    for item in fixtures:
        receipt = current.get_receipt("tenant-a", item["receipt"]["receipt_id"])
        assert ReceiptSigner.verify_trusted(receipt, item["pinned_public_key"])
    # An already-running schema-1 Store can still read immutable historical bytes.
    assert legacy.get_receipt("tenant-a", fixtures[0]["receipt"]["receipt_id"]).schema_version == schema
    # New initialization is idempotent after a committed upgrade.
    Store(connection_settings.storage_dsn)
    assert snapshot(db) == before


def test_future_postgres_schema_refuses_startup_before_writes(connection_settings):
    current = Store(connection_settings.storage_dsn)
    if not connection_settings.database_url:
        assert current.schema_version() == 7
        return
    with current._pg_connect() as con:
        con.execute("INSERT INTO schema_migrations VALUES(999,'future')")
    with pytest.raises(RuntimeError, match="Unsupported database schema"):
        Store(connection_settings.storage_dsn)
    assert current.schema_version() is None
