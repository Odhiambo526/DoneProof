import pytest

from doneproof.domain import CompletionContract
from doneproof.store import Store


def test_contract_ids_are_tenant_scoped(settings):
    store = Store(settings.db_path)
    a = CompletionContract.model_validate(
        {
            "id": "cc_fixed",
            "task": "Task A",
            "postconditions": [
                {
                    "id": "p1",
                    "description": "ok",
                    "provider": "unresolved",
                    "selector": {"state": {"ok": True}},
                    "predicate": {"op": "eq", "path": "ok", "expected": True},
                    "required": True,
                }
            ],
        }
    )
    b = a.model_copy(deep=True)
    b.task = "Task B"
    store.save_contract("tenant-a", a)
    store.save_contract("tenant-b", b)
    assert store.get_contract("tenant-a", "cc_fixed").task == "Task A"
    assert store.get_contract("tenant-b", "cc_fixed").task == "Task B"


def test_contract_content_is_immutable_within_tenant(settings):
    store = Store(settings.db_path)
    a = CompletionContract.model_validate(
        {
            "id": "cc_fixed",
            "task": "Task A",
            "postconditions": [
                {
                    "id": "p1",
                    "description": "ok",
                    "provider": "unresolved",
                    "selector": {"state": {"ok": True}},
                    "predicate": {"op": "eq", "path": "ok", "expected": True},
                    "required": True,
                }
            ],
        }
    )
    store.save_contract("tenant-a", a)
    b = a.model_copy(deep=True)
    b.task = "Task B"
    with pytest.raises(ValueError, match="different content"):
        store.save_contract("tenant-a", b)


def test_stats_include_pilot_metrics(settings):
    import asyncio

    from doneproof.domain import CompletionContract
    from doneproof.engine import VerificationEngine
    from doneproof.signing import ReceiptSigner
    from doneproof.store import Store
    from tests.fakes import MockAdapter

    c = CompletionContract.model_validate(
        {
            "task": "metric",
            "postconditions": [
                {
                    "id": "p1",
                    "description": "ok",
                    "provider": "unresolved",
                    "selector": {"state": {"ok": True}},
                    "predicate": {"op": "eq", "path": "ok", "expected": True},
                    "required": True,
                }
            ],
        }
    )
    store = Store(settings.db_path)
    r = asyncio.run(VerificationEngine({"unresolved": MockAdapter()}, ReceiptSigner(settings)).verify(c))
    store.save_receipt("default", r)
    stats = store.stats("default")
    assert stats["verification_rate"] == 100.0
    assert stats["average_duration_ms"] >= 0
    assert stats["providers"]["unresolved"] == 1


def test_legacy_global_contract_primary_key_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.execute(
        'CREATE TABLE contracts (id TEXT PRIMARY KEY, task TEXT NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL, tenant_id TEXT NOT NULL DEFAULT "default")'
    )
    con.commit()
    con.close()
    store = Store(str(path))
    with store._connect() as con:
        info = con.execute("PRAGMA table_info(contracts)").fetchall()
    pk = [row[1] for row in sorted((r for r in info if r[5]), key=lambda r: r[5])]
    assert pk == ["tenant_id", "id"]
