from doneproof.domain import ConditionStatus, Predicate
from doneproof.predicates import evaluate


def test_eq_contains_and_contains_all():
    state = {"state": "open", "assignees": ["alice", "bob"], "payload": {"amount": 12}}
    assert evaluate(state, Predicate(op="eq", path="state", expected="open"))[0] == ConditionStatus.PASS
    assert evaluate(state, Predicate(op="contains", path="assignees", expected="alice"))[0] == ConditionStatus.PASS
    assert (
        evaluate(state, Predicate(op="contains_all", path="assignees", expected=["alice", "bob"]))[0]
        == ConditionStatus.PASS
    )
    assert evaluate(state, Predicate(op="gte", path="payload.amount", expected=10))[0] == ConditionStatus.PASS


def test_missing_path_fails_but_incomparable_is_unknown():
    assert evaluate({}, Predicate(op="eq", path="missing", expected=1))[0] == ConditionStatus.FAIL
    assert evaluate({"n": "x"}, Predicate(op="gte", path="n", expected=1))[0] == ConditionStatus.UNKNOWN
