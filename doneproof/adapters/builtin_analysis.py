"""Pure contract analysis: no model calls, credentials, network, or signing."""
from __future__ import annotations

import json
import math
import re

from ..compilation_models import issue
from ..security import sanitize

BINDINGS = {"repo", "kind", "number", "title", "author", "head_ref", "base_ref", "message_id", "subject",
            "to", "thread_id", "source", "event_type", "object_id", "assignee", "label", "attachment_name"}
SELECTORS = {
    "github": {"repo", "kind", "number", "title", "author", "head_ref"},
    "gmail": {"message_id", "subject", "to", "thread_id", "location"},
    "webhook": {"source", "event_type", "object_id"},
}
FIELDS = {
    "github": {**dict.fromkeys(["title", "body", "state", "author", "created_at", "updated_at", "closed_at",
                               "head_ref", "base_ref"], str), "number": int, "locked": bool, "draft": bool,
               "merged": bool, "mergeable": bool, "assignees": list, "labels": list},
    "gmail": {**dict.fromkeys(["message_id", "thread_id", "location", "subject", "internal_date"], str),
              **dict.fromkeys(["from", "to", "cc", "bcc", "attachment_names"], list)},
    "webhook": dict.fromkeys(["event_id", "source", "event_type", "object_id", "occurred_at", "payload_hash"], str),
}
SECRET = re.compile(r"(?i)(?:\bBearer\s+\S+|\b(?:sk-|ghp_|gho_|github_pat_)[A-Za-z0-9_-]{8,}|"
                    r"\b(?:access_token|refresh_token|password|client_secret|api_key)\s*[:=])")
ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")


def grounded(value, key, task, context):
    if key == "kind":
        return isinstance(value, str) and value in {"issue", "pull_request"}
    if key == "location":
        return isinstance(value, str) and value in {"sent", "draft", "other"} and value in task.lower()
    if type(context.get(key)) is type(value) and context.get(key) == value:
        return True
    if isinstance(value, str) and not value:
        return False
    literal = json.dumps(value) if type(value) is bool else str(value)
    return bool(re.search(r"(?<![\w.-])" + re.escape(literal) + r"(?![\w.-])", task))


def analyze_condition(pc, task, context):
    out = []
    def add(code, category="unverifiable_outcome", fields=()):
        out.append(issue(code, category, ids=[pc.id], fields=fields))
    if pc.provider not in SELECTORS:
        add("unsupported_provider", "unsupported_provider")
        return out
    if not pc.required:
        add("over_broad_postcondition")
    s = {k: v for k, v in pc.selector.items() if v is not None}
    pc.selector = s
    if set(s) - SELECTORS[pc.provider] or sanitize(s) != s:
        add("impossible_selector")
    for key, value in s.items():
        if key == "number":
            if type(value) is not int or not 0 < value < 2**53:
                add("impossible_selector")
        elif not isinstance(value, str) or not value.strip() or len(value) > 300 or any(ord(x) < 32 for x in value):
            add("impossible_selector")
        if not grounded(value, key, task, context):
            add("ungrounded_identifier", "missing_identifier", [key])
    if pc.provider == "github":
        if not s.get("repo") or not s.get("kind"):
            add("missing_identifier", "missing_identifier", ["repo", "kind"])
        elif not REPO.fullmatch(str(s["repo"])) or not isinstance(s["kind"], str) or s["kind"] not in {"issue", "pull_request"}:
            add("impossible_selector")
        if not s.get("number") and not s.get("title"):
            add("unsafe_discovery", "missing_identifier", ["number", "title"])
        if s.get("kind") != "pull_request" and s.get("head_ref"):
            add("impossible_selector")
        if s.get("number") and any(s.get(x) for x in ("title", "author", "head_ref")):
            # Direct lookup ignores discovery constraints: never pretend these were enforced.
            add("impossible_selector")
    elif pc.provider == "gmail":
        if s.get("message_id"):
            if not ID.fullmatch(str(s["message_id"])) or len(s) != 1:
                add("impossible_selector")
        else:
            if not s.get("subject") or not s.get("to"):
                add("unsafe_discovery", "missing_identifier", ["message_id", "subject", "to"])
            if s.get("to") and not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", str(s["to"])):
                add("impossible_selector")
            if s.get("thread_id") and not ID.fullmatch(str(s["thread_id"])):
                add("impossible_selector")
            if s.get("location") is not None:
                # Outcome-based filtering could hide conflicting/ambiguous messages.
                add("unsafe_discovery")
    else:
        missing = [x for x in ("source", "event_type", "object_id") if not s.get(x)]
        if missing:
            add("missing_identifier", "missing_identifier", missing)
        for key in ("source", "event_type", "object_id"):
            if s.get(key) and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", str(s[key])):
                add("impossible_selector")
    p = pc.predicate
    values = p.expected if isinstance(p.expected, list) else [p.expected]
    aliases = {"assignees": "assignee", "labels": "label", "attachment_names": "attachment_name"}
    for value in values:
        derived = ((p.path, value) in [("state", "closed"), ("state", "open"), ("location", "sent")]
                   and re.search({"closed": r"\bclos(?:e|ed)\b", "open": r"\b(?:reopen|open)\b",
                                  "sent": r"\b(?:send|sent|email|mail)\b"}[value], task, re.I)) if type(value) is str else False
        if type(value) is bool:
            verbs = {"merged": "merge|merged", "locked": "lock|locked" if value else "unlock|unlocked",
                     "draft": "draft"}
            derived = p.path in verbs and re.search(r"\b(?:" + verbs[p.path] + r")\b", task, re.I)
        if value is not None and not derived and not grounded(value, aliases.get(p.path, p.path), task, context):
            add("over_broad_postcondition")
    field_type = FIELDS[pc.provider].get(p.path)
    if pc.provider == "github" and s.get("kind") == "issue" and p.path in {"draft", "merged", "mergeable", "head_ref", "base_ref"}:
        field_type = None
    if pc.provider == "webhook" and re.fullmatch(r"payload\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*", p.path):
        field_type = type(p.expected) if type(p.expected) in (str, int, float, bool) else None
        if sanitize({segment: "value" for segment in p.path.split(".")}) != {segment: "value" for segment in p.path.split(".")}:
            field_type = None
    if not field_type or p.op in {"exists", "not_exists"} or p.expected is None:
        add("meaningless_predicate")
    elif p.op in {"eq", "neq"} and type(p.expected) is not field_type:
        add("meaningless_predicate")
    elif p.op in {"contains", "contains_all"}:
        values = p.expected if p.op == "contains_all" else [p.expected]
        if field_type is not list or not isinstance(values, list) or not values or any(type(v) is not str or not v.strip() for v in values):
            add("meaningless_predicate")
    elif p.op in {"gte", "lte"} and (field_type not in (int, float) or type(p.expected) not in (int, float)):
        add("meaningless_predicate")
    if type(p.expected) is float and not math.isfinite(p.expected):
        add("meaningless_predicate")
    if p.expected in ("", []) or SECRET.search(json.dumps(p.expected)):
        add("meaningless_predicate")
    enum = {("github", "state"): {"open", "closed"}, ("gmail", "location"): {"sent", "draft", "other"}}.get((pc.provider, p.path))
    if enum and (not isinstance(p.expected, str) or p.expected not in enum):
        add("meaningless_predicate")
    if pc.require_change and p.path in {"number", "message_id", "thread_id", "created_at", "author", "event_id", "object_id"}:
        add("meaningless_predicate")
    return out



def analyze_intent(intent, targets):
    problems = []
    unquoted = re.sub(r'"[^"\n]*"', "", intent.source_text)
    lower = unquoted.lower()
    required_actions = {
        "close": ("state", "closed"), "reopen": ("state", "open"), "merge": ("merged", True),
        "lock": ("locked", True), "unlock": ("locked", False),
    }
    if intent.mode == "state":
        required_actions.update({"closed": ("state", "closed"), "open": ("state", "open"),
                                 "merged": ("merged", True), "locked": ("locked", True),
                                 "unlocked": ("locked", False), "draft": ("draft", True)})
    for verb, (path, value) in required_actions.items():
        if re.search(r"\b" + verb + r"\b", lower) and any(pc.provider == "github" for pc in targets):
            if not any(pc.predicate.path == path and pc.predicate.op == "eq"
                       and pc.predicate.expected == value for pc in targets):
                problems.append(issue("over_broad_postcondition", ids=intent.condition_ids))
    # Unsupported promises cannot be represented by resource existence or delivery metadata.
    if re.search(r"\b(read by|opened by|understood|satisfied|happy|correctly|bug.free|approved|approval|"
                 r"review approval|email body|message body|bcc privacy|exactly once|without notifying|"
                 r"unless|only if|except when)\b", lower):
        problems.append(issue("unsupported_outcome", ids=intent.condition_ids))
    if re.search(r"\b(send|email|mail)\b", lower) and any(pc.provider == "gmail" for pc in targets):
        if not any(pc.predicate.path == "location" and pc.predicate.op == "eq"
                   and pc.predicate.expected == "sent" for pc in targets):
            problems.append(issue("over_broad_postcondition", ids=intent.condition_ids))
    if intent.mode == "state" and any(pc.provider == "gmail" for pc in targets):
        for location in ("sent", "draft"):
            if re.search(r"\b" + location + r"\b", lower) and not any(
                pc.predicate.path == "location" and pc.predicate.op == "eq" and pc.predicate.expected == location
                for pc in targets
            ):
                problems.append(issue("over_broad_postcondition", ids=intent.condition_ids))
    return problems
