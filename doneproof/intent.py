"""Exact, full-clause grammars. Unconsumed text always leaves the fast path."""
from __future__ import annotations

import re

from .compilation_models import Candidate, Intent
from .domain import Postcondition, Predicate

REPO = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
RESOURCE = rf"(?P<kind>issue|pull request|PR) #(?P<number>[0-9]+) in (?P<repo>{REPO})"
Q = r'"([^"\n]+)"'


def clauses(task):
    # Separators inside quoted titles/subjects are data, not additional intents.
    return [part.strip().rstrip(".") for part in
            re.split(r';(?=(?:[^"]*"[^"]*")*[^"]*$)|\n', task) if part.strip()]


def fast_candidate(task: str, context: dict | None = None) -> Candidate | None:
    intents, conditions = [], []
    for clause in clauses(task):
        normalized = re.sub(r"(?i)^please\s+", "", clause)
        parsed = parse_clause(normalized)
        if parsed is None and context:
            bound = normalized
            repo = context.get("repo")
            if isinstance(repo, str) and re.fullmatch(REPO, repo):
                bound = re.sub(r"(?i)((?:issue|PR|pull request) #[0-9]+)(?![0-9]| in )", r"\g<1> in " + repo, bound)
            if re.fullmatch(r"(?i)Send (?:an? )?(?:email|message)", bound):
                subject, to = context.get("subject"), context.get("to")
                if all(isinstance(x, str) and not re.search(r'["\n\r]', x) for x in (subject, to)):
                    bound = f'Send email to {to} with subject "{subject}"'
            parsed = parse_clause(bound)
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


def parse_clause(clause):
    def match(pattern):
        return re.fullmatch(pattern, clause, re.IGNORECASE)

    def github(m, predicates, mode="transition"):
        return "github", {"repo": m["repo"], "kind": "issue" if m["kind"].lower() == "issue" else "pull_request",
                          "number": int(m["number"])}, predicates, mode

    if m := match(rf"(Close|Reopen|Merge|Lock|Unlock) {RESOURCE}"):
        verb = m[1].lower()
        field, value = {"close": ("state", "closed"), "reopen": ("state", "open"),
                        "merge": ("merged", True), "lock": ("locked", True), "unlock": ("locked", False)}[verb]
        return github(m, [("eq", field, value)])
    if m := match(rf"Assign {RESOURCE} to (?P<login>[A-Za-z0-9-]+)"):
        return github(m, [("contains", "assignees", m["login"])])
    if m := match(rf'Add label (?P<label>"[^"\n]+") to {RESOURCE}'):
        return github(m, [("contains", "labels", m["label"][1:-1])])
    if m := match(rf'Rename {RESOURCE} to (?P<title>"[^"\n]+")'):
        return github(m, [("eq", "title", m["title"][1:-1])])
    if m := match(rf"(?:Verify|Check) {RESOURCE} is (open|closed|merged|locked|unlocked|draft)"):
        field, value = {"open": ("state", "open"), "closed": ("state", "closed"), "merged": ("merged", True),
                        "locked": ("locked", True), "unlocked": ("locked", False), "draft": ("draft", True)}[m[4].lower()]
        return github(m, [("eq", field, value)], "state")
    if m := match(rf'Create (?:an? )?issue in ({REPO}) titled {Q}'):
        return "github", {"repo": m[1], "kind": "issue", "title": m[2]}, [("eq", "title", m[2])], "create"
    if m := match(rf'Create (?:an? )?pull request in ({REPO}) titled {Q} from {Q} to {Q}'):
        return "github", {"repo": m[1], "kind": "pull_request", "title": m[2], "head_ref": m[3]}, [
            ("eq", "title", m[2]), ("eq", "head_ref", m[3]), ("eq", "base_ref", m[4])], "create"
    if m := match(rf'Close issue in ({REPO}) titled {Q}'):
        return "github", {"repo": m[1], "kind": "issue", "title": m[2]}, [("eq", "state", "closed")], "transition"
    if m := match(rf'Send (?:an? )?(?:email|message) to ([^\s";]+@[^\s";]+) with subject {Q}(?: with attachment {Q})?'):
        predicates = [("eq", "location", "sent"), ("eq", "subject", m[2]), ("contains", "to", m[1])]
        if m[3]:
            predicates.append(("contains_all", "attachment_names", [m[3]]))
        return "gmail", {"subject": m[2], "to": m[1]}, predicates, "create"
    if m := match(r"Send Gmail draft ([A-Za-z0-9_-]+)"):
        return "gmail", {"message_id": m[1]}, [("eq", "location", "sent")], "transition"
    if m := match(r"(?:Verify|Check) Gmail message ([A-Za-z0-9_-]+) is (sent|draft)"):
        return "gmail", {"message_id": m[1]}, [("eq", "location", m[2].lower())], "state"
    if m := match(rf'(?:Verify|Check) Gmail message with subject {Q} to ([^\s";]+@[^\s";]+) is (sent|draft)'):
        return "gmail", {"subject": m[1], "to": m[2]}, [("eq", "location", m[3].lower())], "state"
    if m := match(rf'(?:Verify|Check) Gmail message ([A-Za-z0-9_-]+) has attachment {Q}'):
        return "gmail", {"message_id": m[1]}, [("contains_all", "attachment_names", [m[2]])], "state"
    if m := match(rf'Wait for webhook {Q} from {Q} for object {Q}(?: with (payload\.[A-Za-z0-9_.]+) = {Q})?'):
        predicates = [("eq", "event_type", m[1]), ("eq", "object_id", m[3])]
        if m[4]:
            predicates.append(("eq", m[4], m[5]))
        return "webhook", {"event_type": m[1], "source": m[2], "object_id": m[3]}, predicates, "event"
    return None
