from doneproof.domain import ConditionStatus, Predicate
from doneproof.predicates import evaluate


def test_eq_and_contains_all():
    state = {"state": "open", "assignees": ["alice", "bob"]}
    s, _, _ = evaluate(state, Predicate(op="eq", path="state", expected="open"))
    assert s == ConditionStatus.PASS
    s, _, _ = evaluate(state, Predicate(op="contains_all", path="assignees", expected=["alice", "bob"]))
    assert s == ConditionStatus.PASS


def test_missing_path_fails():
    s, reason, _ = evaluate({}, Predicate(op="eq", path="missing", expected=1))
    assert s == ConditionStatus.FAIL
    assert "does not exist" in reason
