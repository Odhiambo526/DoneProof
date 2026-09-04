from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .domain import ConditionStatus, Predicate

_MISSING = object()


def resolve_path(value: Any, path: str) -> Any:
    if not path:
        return value
    cur = value
    for raw_part in path.split("."):
        part = raw_part.strip()
        if isinstance(cur, Mapping):
            if part not in cur:
                return _MISSING
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            if idx >= len(cur):
                return _MISSING
            cur = cur[idx]
        else:
            return _MISSING
    return cur


def evaluate(state: Any, predicate: Predicate) -> tuple[ConditionStatus, str, Any]:
    actual = resolve_path(state, predicate.path)
    op = predicate.op
    expected = predicate.expected

    if op == "exists":
        ok = actual is not _MISSING and actual is not None
    elif op == "not_exists":
        ok = actual is _MISSING or actual is None
    elif actual is _MISSING:
        return ConditionStatus.FAIL, f"path '{predicate.path}' does not exist", None
    elif op == "eq":
        ok = actual == expected
    elif op == "neq":
        ok = actual != expected
    elif op == "contains":
        try:
            ok = expected in actual
        except TypeError:
            ok = False
    elif op == "contains_all":
        try:
            ok = all(x in actual for x in expected)
        except (TypeError, ValueError):
            ok = False
    elif op == "gte":
        try:
            ok = actual >= expected
        except TypeError:
            return ConditionStatus.UNKNOWN, "values are not comparable", actual
    elif op == "lte":
        try:
            ok = actual <= expected
        except TypeError:
            return ConditionStatus.UNKNOWN, "values are not comparable", actual
    else:
        return ConditionStatus.UNKNOWN, f"unsupported predicate: {op}", actual

    status = ConditionStatus.PASS if ok else ConditionStatus.FAIL
    return status, f"observed={actual!r}; expected {op} {expected!r}", actual
