"""Exact, full-clause grammars. Unconsumed text always leaves the fast path."""
from __future__ import annotations

import re

from .compilation_models import Candidate, Intent
from .domain import Postcondition, Predicate
from .provider_registry import default_registry


def clauses(task):
    # Separators inside quoted titles/subjects are data, not additional intents.
    return [part.strip().rstrip(".") for part in
            re.split(r';(?=(?:[^"]*"[^"]*")*[^"]*$)|\n', task) if part.strip()]


def fast_candidate(task: str, context: dict | None = None, registry=None) -> Candidate | None:
    registry = registry or default_registry()
    intents, conditions = [], []
    for clause in clauses(task):
        normalized = re.sub(r"(?i)^please\s+", "", clause)
        matches = [value for d in registry
                   if (value := d.compiler.parse_clause(normalized, context or {})) is not None]
        parsed = matches[0] if len(matches) == 1 else None
        if parsed is None:
            return None
        provider, selector, predicates, mode = parsed
        ids = []
        for op, path, expected in predicates:
            ident = f"p{len(conditions) + 1}"
            ids.append(ident)
            conditions.append(Postcondition(id=ident, description=f"Requested {provider} {path or 'resource'} outcome",
                provider=provider, selector=selector, predicate=Predicate(op=op, path=path, expected=expected),
                required=True, require_change=mode == "transition"))
        intents.append(Intent(source_text=clause, mode=mode, condition_ids=ids))
    if not conditions or len(conditions) > 50:
        return None
    return Candidate(intents=intents, postconditions=conditions)
