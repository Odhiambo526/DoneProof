from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest

from doneproof.domain import CompletionContract
from doneproof.engine import VerificationEngine
from doneproof.signing import ReceiptSigner
from doneproof.store import Store
from tests.fakes import MockAdapter

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not configured")


@pytest.fixture
def pg_store():
    store = Store(os.environ["TEST_DATABASE_URL"])
    # Use isolated tenant/object IDs so the CI database can be reused safely.
    return store


def _contract(task: str = "Postgres verification") -> CompletionContract:
    return CompletionContract.model_validate(
        {
            "task": task,
            "postconditions": [
                {
                    "id": "p1",
                    "description": "state is correct",
                    "provider": "unresolved",
                    "selector": {"state": {"ok": True}},
                    "predicate": {"op": "eq", "path": "ok", "expected": True},
                    "required": True,
                }
            ],
        }
    )


def test_postgres_contract_receipt_and_stats(pg_store, settings):
    tenant = "pg-contract"
    contract = _contract()
    pg_store.save_contract(tenant, contract)
    assert pg_store.get_contract(tenant, contract.id).task == contract.task

    receipt = asyncio.run(
        VerificationEngine({"unresolved": MockAdapter()}, ReceiptSigner(settings)).verify(contract)
    )
    pg_store.save_receipt(tenant, receipt)
    loaded = pg_store.get_receipt(tenant, receipt.receipt_id)
    assert loaded is not None and loaded.receipt_hash == receipt.receipt_hash
    assert pg_store.stats(tenant)["verification_rate"] == 100.0


def test_postgres_contract_immutability_and_tenant_scope(pg_store):
    contract = _contract("Tenant A")
    contract.id = "cc_pg_fixed"
    pg_store.save_contract("pg-a", contract)
    other = contract.model_copy(deep=True)
    other.task = "Tenant B"
    pg_store.save_contract("pg-b", other)
    assert pg_store.get_contract("pg-b", contract.id).task == "Tenant B"

    changed = contract.model_copy(deep=True)
    changed.task = "Mutated"
    with pytest.raises(ValueError, match="different content"):
        pg_store.save_contract("pg-a", changed)


def test_postgres_baseline_idempotency_webhook_and_audit(pg_store, settings):
    tenant = "pg-ops"
    contract = _contract("Operations")
    contract.postconditions[0].require_change = True
    result = asyncio.run(
        VerificationEngine({"unresolved": MockAdapter()}, ReceiptSigner(settings)).snapshot(contract)
    )[0]
    pg_store.save_baseline(tenant, contract.id, result)
    assert pg_store.get_baselines(tenant, contract.id)["p1"].id == "p1"

    receipt = asyncio.run(
        VerificationEngine({"unresolved": MockAdapter()}, ReceiptSigner(settings)).verify(contract)
    )
    pg_store.save_receipt(tenant, receipt)
    pg_store.save_idempotency(tenant, "idem-pg", "hash-pg", receipt.receipt_id)
    assert pg_store.get_idempotency(tenant, "idem-pg")["receipt_id"] == receipt.receipt_id

    occurred = datetime.now(timezone.utc)
    inserted, payload_hash = pg_store.save_event(
        tenant, "erp", "refund.completed", "refund-1", occurred, {"status": "completed"}, "evt_pg_1"
    )
    duplicate, duplicate_hash = pg_store.save_event(
        tenant, "erp", "refund.completed", "refund-1", occurred, {"status": "completed"}, "evt_pg_1"
    )
    assert inserted is True and duplicate is False and payload_hash == duplicate_hash
    events = pg_store.find_events(tenant, "erp", "refund.completed", "refund-1", occurred)
    assert events and events[0]["payload"]["status"] == "completed"

    pg_store.audit(tenant, "verification.completed", "receipt", receipt.receipt_id, {"verdict": "VERIFIED"})
    assert pg_store.list_audit(tenant, 1)[0]["action"] == "verification.completed"


def test_postgres_production_app_starts_ready(settings):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from doneproof.app import create_app

    prod = replace(
        settings,
        env="production",
        api_keys={"prod-key": "acme"},
        database_url=os.environ["TEST_DATABASE_URL"],
    )
    client = TestClient(create_app(prod, adapter_overrides={"unresolved": MockAdapter()}))
    ready = client.get("/ready").json()
    assert ready["ready"] is True
    assert ready["storage_backend"] == "postgresql"
    assert ready["durable_storage"] is True
    assert "Strict-Transport-Security" in client.get("/health").headers
