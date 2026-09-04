import pytest

from doneproof.domain import CompletionContract
from doneproof.store import Store


def test_contract_id_cannot_be_overwritten_across_tenants(settings):
    store=Store(settings.db_path)
    c=CompletionContract.model_validate({"id":"cc_fixed","task":"Task A","postconditions":[{"id":"p1","description":"ok","provider":"mock","selector":{"state":{"ok":True}},"predicate":{"op":"eq","path":"ok","expected":True},"required":True}]})
    store.save_contract('tenant-a',c)
    with pytest.raises(ValueError,match='another workspace'):
        store.save_contract('tenant-b',c)
    assert store.get_contract('tenant-a','cc_fixed').task == 'Task A'
    assert store.get_contract('tenant-b','cc_fixed') is None
