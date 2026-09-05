"""The original GitHub, Gmail and webhook exact grammars."""
import re

REPO = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
RESOURCE = rf"(?P<kind>issue|pull request|PR) #(?P<number>[0-9]+) in (?P<repo>{REPO})"
Q = r'"([^"\n]+)"'


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
