"""Pure contract analysis: no model calls, credentials, network, or signing."""
from __future__ import annotations

import json
import re
from itertools import combinations

from .compilation_models import Candidate, issue
from .intent import fast_candidate
from .provider_registry import default_registry
from .security import sanitize

SECRET = re.compile(r"(?i)(?:\bBearer\s+\S+|\b(?:sk-|ghp_|gho_|github_pat_)[A-Za-z0-9_-]{8,}|"
                    r"\b(?:access_token|refresh_token|password|client_secret|api_key)\s*[:=])")
ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")
MUTATIONS = re.compile(r"(?i)\b(close|reopen|merge|assign|rename|lock|unlock|update|change|remove|delete|approve|attach|mark|move)\b")


def safe_context(context, registry=None):
    registry = registry or default_registry()
    bindings = {key for d in registry for key in d.manifest.context_fields}
    return {k: v for k, v in context.items() if k in bindings and type(v) in (str, int)
            and len(str(v)) <= 500}


def sensitive(task, context):
    # Unknown context objects (including executor claims) are never sent to the model.
    return sanitize(context) != context or bool(SECRET.search(task + json.dumps(context)))


def signature(pc):
    return (pc.provider, json.dumps(pc.selector, sort_keys=True), pc.predicate.path,
            pc.predicate.op, json.dumps(pc.predicate.expected, sort_keys=True))


def grounded(value, key, task, context):
    if type(context.get(key)) is type(value) and context.get(key) == value:
        return True
    if isinstance(value, str) and not value:
        return False
    literal = json.dumps(value) if type(value) is bool else str(value)
    return bool(re.search(r"(?<![\w.-])" + re.escape(literal) + r"(?![\w.-])", task))


def analyze(candidate: Candidate, task: str, context: dict, registry=None):
    registry = registry or default_registry()
    problems = []
    for pc in candidate.postconditions:
        pc.selector = {k: v for k, v in pc.selector.items() if v is not None}
    by_id = {pc.id: pc for pc in candidate.postconditions}
    if len(by_id) != len(candidate.postconditions):
        problems.append(issue("duplicate_conditions"))
    referenced = []
    sources = {}
    cursor = 0
    for intent in candidate.intents:
        pos = task.find(intent.source_text, cursor)
        gap = task[cursor:pos] if pos >= 0 else "INVALID"
        if pos < 0 or re.sub(r"(?i)\b(?:and|then)\b|[\s.;]", "", gap):
            problems.append(issue("incomplete_intent"))
        cursor = pos + len(intent.source_text) if pos >= 0 else cursor
        referenced.extend(intent.condition_ids)
        targets = [by_id[x] for x in intent.condition_ids if x in by_id]
        sources.update({x: intent.source_text for x in intent.condition_ids})
        if len(targets) != len(intent.condition_ids) or intent.mode == "unverifiable":
            problems.append(issue("unsupported_outcome", ids=intent.condition_ids))
        unquoted = re.sub(r'"[^"\n]*"', "", intent.source_text)
        if intent.mode == "transition" or MUTATIONS.search(unquoted):
            if not targets or any(not pc.require_change for pc in targets):
                problems.append(issue("missing_transition", ids=intent.condition_ids))
        if intent.mode == "state" and not re.match(r"(?i)^(?:please )?(?:verify|check|confirm)\b", unquoted):
            problems.append(issue("over_broad_postcondition", ids=intent.condition_ids))
        # A model cannot reinterpret an exact supported clause more weakly than the grammar.
        expected = fast_candidate(intent.source_text, context, registry)
        if expected and ({signature(pc) for pc in expected.postconditions} != {signature(pc) for pc in targets}
                         or expected.intents[0].mode != intent.mode):
            problems.append(issue("over_broad_postcondition", ids=intent.condition_ids))
        if len(set(re.findall(r"#[0-9]+\b", unquoted))) > 1:
            problems.append(issue("incomplete_intent", ids=intent.condition_ids))
        represented = json.dumps([{"selector": pc.selector, "expected": pc.predicate.expected} for pc in targets])
        for literal in re.findall(r'"([^"\n]+)"', intent.source_text):
            if json.dumps(literal)[1:-1] not in represented:
                problems.append(issue("over_broad_postcondition", ids=intent.condition_ids))
        for definition in registry:
            own = [pc for pc in targets if pc.provider == definition.manifest.provider_id]
            if own:
                problems.extend(definition.compiler.analyze_intent(intent, own))
    if re.sub(r"[\s.;]", "", task[cursor:]) or sorted(referenced) != sorted(by_id) or len(referenced) != len(set(referenced)):
        problems.append(issue("incomplete_intent"))
    for pc in candidate.postconditions:
        problems.extend(analyze_condition(pc, sources.get(pc.id, ""), context, registry))
    problems.extend(consistency_problems(candidate))
    # Stable, bounded diagnostics without repeating the same rule per code/condition.
    return list({(p.code, tuple(p.condition_ids), tuple(p.fields)): p for p in problems}.values())


def consistency_problems(candidate):
    problems, seen = [], {}
    for pc in candidate.postconditions:
        sig = signature(pc)
        if sig in seen:
            problems.append(issue("duplicate_conditions", ids=[seen[sig], pc.id]))
        seen[sig] = pc.id
    for a, b in combinations(candidate.postconditions, 2):
        if a.provider == b.provider and a.selector == b.selector and a.predicate.path == b.predicate.path:
            if contradictory(a.predicate, b.predicate):
                problems.append(issue("contradictory_predicates", ids=[a.id, b.id]))
    return problems


def analyze_condition(pc, task, context, registry=None):
    definition = (registry or default_registry()).get(pc.provider)
    if definition is None:
        return [issue("unsupported_provider", "unsupported_provider", ids=[pc.id])]
    problems = definition.compiler.analyze_condition(pc, task, context)
    spec = definition.manifest
    if pc.predicate.op not in spec.supported_predicates:
        problems.append(issue("meaningless_predicate", ids=[pc.id]))
    if pc.require_change and not spec.transition_support:
        problems.append(issue("missing_transition", ids=[pc.id]))
    if any(pc.predicate.path == path or pc.predicate.path.startswith(path + ".")
           or path.startswith(pc.predicate.path + ".") for path in spec.sensitive_paths):
        problems.append(issue("meaningless_predicate", ids=[pc.id]))
    return problems


def contradictory(a, b):
    x, y = a.expected, b.expected
    if a.op == b.op == "eq":
        return type(x) is not type(y) or x != y
    if a.op != "eq" and b.op == "eq":
        return contradictory(b, a)
    if a.op == "eq":
        if b.op == "neq":
            return x == y
        if b.op == "contains" and isinstance(x, list):
            return y not in x
        if b.op == "contains_all" and isinstance(x, list) and isinstance(y, list):
            return not all(item in x for item in y)
        if type(x) in (int, float) and type(y) in (int, float):
            return (b.op == "gte" and x < y) or (b.op == "lte" and x > y)
    if a.op == "lte" and b.op == "gte":
        return contradictory(b, a)
    return a.op == "gte" and b.op == "lte" and type(x) in (int, float) and type(y) in (int, float) and x > y
