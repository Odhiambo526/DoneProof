"""Reviewed intent/condition pairs; deliberately independent of compiler parsing.

These fixtures specify requested outcomes, not executor evidence. The runner supplies
their worlds through an authoritative adapter double and tests both success and failure.
"""
from __future__ import annotations

import copy


def condition(provider, selector, path, expected, change=False, op="eq"):
    return dict(provider=provider, selector=selector, path=path, expected=expected, op=op, require_change=change)


def valid(task, provider, selector, goals, *, before=None, future=False, context=None, lookup=None):
    after = ({"number": selector.get("number", 701), "title": selector.get("title", "Customer issue"),
              "state": "open", "locked": False, "merged": False, "draft": False,
              "labels": [], "assignees": [], "head_ref": "feature", "base_ref": "main"} if provider == "github"
             else {"message_id": selector.get("message_id", "new_msg_701"), "thread_id": "thread7",
                   "subject": selector.get("subject", "Customer report"), "to": [selector.get("to", "ana@example.com")],
                   "location": "draft", "attachment_names": []} if provider == "gmail"
             else {"event_id": "evt701", **selector, "payload": {}, "occurred_at": "2026-09-05T12:00:00Z"})
    for goal in goals:
        path, value = goal["path"], goal["expected"]
        target = after
        parts = path.split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = [value] if goal["op"] == "contains" else copy.deepcopy(value)
    previous = {**copy.deepcopy(after), **(before or {})}
    return {"task": task, "provider": provider, "expected_status": "valid_contract", "expected_conditions": goals,
            "context": context or {}, "resources": [{"provider": provider, "lookup": lookup or selector,
                "before": previous, "after": after, "future": future}], "connection": "available"}


def gh(task, number, path, expected, *, repo="acme/api", kind="issue", before=None, op="eq", change=True, context=None):
    selector = {"repo": repo, "kind": kind, "number": number}
    return valid(task, "github", selector, [condition("github", selector, path, expected, change, op)],
                 before=before, context=context)


def mail(task, ident, path, expected, *, before=None, change=False, op="eq"):
    selector = {"message_id": ident}
    return valid(task, "gmail", selector, [condition("gmail", selector, path, expected, change, op)], before=before)


def send(task, recipient, subject, attachment=None, context=None):
    selector = {"subject": subject, "to": recipient}
    goals = [condition("gmail", selector, "location", "sent"), condition("gmail", selector, "subject", subject),
             condition("gmail", selector, "to", recipient, op="contains")]
    if attachment:
        goals.append(condition("gmail", selector, "attachment_names", [attachment], op="contains_all"))
    return valid(task, "gmail", selector, goals, future=True, context=context)


def hook(task, source, event, obj, payload=None):
    selector = {"source": source, "event_type": event, "object_id": obj}
    goals = [condition("webhook", selector, "event_type", event), condition("webhook", selector, "object_id", obj)]
    if payload:
        goals.append(condition("webhook", selector, payload[0], payload[1]))
    return valid(task, "webhook", selector, goals, future=True)


def invalid(provider, task, status="unverifiable_outcome", **extra):
    return {"provider": provider, "task": task, "expected_status": status, "expected_conditions": [],
            "resources": [], "context": {}, "connection": "available", **extra}


def corpus():
    cases = [
        gh("Close issue #42 in acme/api", 42, "state", "closed", before={"state": "open"}),
        gh("Close issue #18 in northstar/checkout", 18, "state", "closed", repo="northstar/checkout", before={"state": "open"}),
        gh("Reopen issue #73 in acme/api", 73, "state", "open", before={"state": "closed"}),
        gh("Reopen issue #51 in orbit/mobile", 51, "state", "open", repo="orbit/mobile", before={"state": "closed"}),
        gh("Merge PR #28 in acme/api", 28, "merged", True, kind="pull_request", before={"merged": False}),
        gh("Merge pull request #107 in northstar/checkout", 107, "merged", True, repo="northstar/checkout", kind="pull_request", before={"merged": False}),
        gh("Lock issue #8 in acme/api", 8, "locked", True, before={"locked": False}),
        gh("Unlock issue #8 in acme/api", 8, "locked", False, before={"locked": True}),
        gh("Assign issue #42 in acme/api to maya", 42, "assignees", "maya", before={"assignees": []}, op="contains"),
        gh("Assign PR #28 in acme/api to leo", 28, "assignees", "leo", kind="pull_request", before={"assignees": []}, op="contains"),
        gh('Add label "release ready" to PR #28 in acme/api', 28, "labels", "release ready", kind="pull_request", before={"labels": []}, op="contains"),
        gh('Add label "customer escalation" to issue #8 in acme/api', 8, "labels", "customer escalation", before={"labels": []}, op="contains"),
        gh('Rename issue #42 in acme/api to "Fix checkout timeout"', 42, "title", "Fix checkout timeout", before={"title": "Investigate checkout"}),
        gh('Rename PR #28 in acme/api to "Release 0.9.5"', 28, "title", "Release 0.9.5", kind="pull_request", before={"title": "Draft release"}),
        gh("Verify issue #42 in acme/api is closed", 42, "state", "closed", change=False),
        gh("Check issue #73 in acme/api is open", 73, "state", "open", change=False),
        gh("Verify PR #28 in acme/api is merged", 28, "merged", True, kind="pull_request", change=False),
        gh("Check PR #107 in northstar/checkout is draft", 107, "draft", True, repo="northstar/checkout", kind="pull_request", change=False),
        gh("Verify issue #8 in acme/api is unlocked", 8, "locked", False, change=False),
        gh("Check issue #8 in acme/api is locked", 8, "locked", True, change=False),
        gh("Close issue #42", 42, "state", "closed", before={"state": "open"}, context={"repo": "acme/api"}),
        gh("Mark issue #42 in acme/api as closed", 42, "state", "closed", before={"state": "open"}),
        gh("Get PR #28 in acme/api merged", 28, "merged", True, kind="pull_request", before={"merged": False}),
        gh("Have maya take ownership of issue #42 in acme/api", 42, "assignees", "maya", before={"assignees": []}, op="contains"),
    ]
    for title, repo in [("Investigate cart abandonment", "acme/api"), ("Document retry policy", "orbit/mobile"),
                        ("Update French locale", "northstar/checkout"), ("Release; follow up", "acme/api")]:
        selector = {"repo": repo, "kind": "issue", "title": title}
        cases.append(valid(f'Create issue in {repo} titled "{title}"', "github", selector,
                           [condition("github", selector, "title", title)], future=True))
    for title, branch in [("Ship billing fix", "fix/billing"), ("Release 0.9.5", "release/0.9.5")]:
        selector = {"repo": "acme/api", "kind": "pull_request", "title": title, "head_ref": branch}
        cases.append(valid(f'Create pull request in acme/api titled "{title}" from "{branch}" to "main"', "github", selector,
            [condition("github", selector, "title", title), condition("github", selector, "head_ref", branch),
             condition("github", selector, "base_ref", "main")], future=True))
    for count, status in [(0, "missing_identifier"), (1, "valid_contract"), (2, "ambiguous_resource")]:
        selector = {"repo": "acme/api", "kind": "issue", "number": 701}
        item = valid('Close issue in acme/api titled "Checkout regression"', "github", selector,
            [condition("github", selector, "state", "closed", True)], before={"state": "open", "title": "Checkout regression"})
        item["resources"][0]["after"]["title"] = "Checkout regression"
        item["resources"] *= count
        item["expected_status"] = status
        if count != 1:
            item["expected_conditions"] = []
        cases.append(item)
    cases.extend([
        invalid("github", "Close issue", "missing_identifier"),
        invalid("github", "Merge PR", "missing_identifier"),
        invalid("github", "Approve PR #28 in acme/api"),
        invalid("github", "Make the code in acme/api bug-free"),
        invalid("github", "Close issue #42 in acme/api; Reopen issue #42 in acme/api"),
        invalid("github", "Close issue #42 in acme/api; Close issue #42 in acme/api"),
        invalid("github", "Merge issue #42 in acme/api"),
        invalid("github", "Create a Jira issue for the checkout regression", "unsupported_provider"),
        invalid("github", "Close issue #42 in acme/api without notifying anyone"),
        invalid("github", "Close issue #42 in acme/api", connection="disabled"),
        invalid("github", "Close issue #42 in acme/api", context={"access_token": "test-secret-sentinel"}),
    ])
    cases.extend([
        mail("Verify Gmail message msg101 is sent", "msg101", "location", "sent"),
        mail("Check Gmail message msg102 is draft", "msg102", "location", "draft"),
        mail("Verify Gmail message msg103 is sent", "msg103", "location", "sent"),
        mail("Check Gmail message msg104 is draft", "msg104", "location", "draft"),
        mail("Send Gmail draft msg105", "msg105", "location", "sent", change=True, before={"location": "draft"}),
        mail("Send Gmail draft msg106", "msg106", "location", "sent", change=True, before={"location": "draft"}),
        mail('Verify Gmail message msg107 has attachment "invoice.pdf"', "msg107", "attachment_names", ["invoice.pdf"], op="contains_all"),
        mail('Check Gmail message msg108 has attachment "rollout.csv"', "msg108", "attachment_names", ["rollout.csv"], op="contains_all"),
        mail('Verify Gmail message msg109 has attachment "Board pack Q3.pdf"', "msg109", "attachment_names", ["Board pack Q3.pdf"], op="contains_all"),
        mail("Confirm that message msg101 in Gmail is in Sent", "msg101", "location", "sent"),
        mail("Move Gmail draft msg105 to Sent", "msg105", "location", "sent", change=True, before={"location": "draft"}),
    ])
    for recipient, subject, attachment in [
        ("ana@example.com", "Q3 report", "report.pdf"), ("finance@example.org", "Invoice 482", "invoice-482.pdf"),
        ("ops@example.com", "Rollout checklist", "checklist.csv"), ("maya@example.net", "Meeting notes", None),
        ("legal@example.com", "Signed agreement", "agreement.pdf"), ("support@example.org", "Case 221 update", None),
        ("sales@example.com", "Renewal quote", "quote.pdf"), ("oncall@example.net", "Incident summary", None),
        ("board@example.com", "Board pack", "Q3.pdf"), ("recruiting@example.org", "Interview availability", None),
        ("billing@example.com", "Credit note", "credit.pdf"), ("partner@example.net", "Design partner update", None),
        ("ana@example.com", "Report; revised", None), ("ops+release@example.com", "Release 0.9.5", "notes.md"),
    ]:
        task = f'Send email to {recipient} with subject "{subject}"'
        if attachment:
            task += f' with attachment "{attachment}"'
        cases.append(send(task, recipient, subject, attachment))
    cases.append(send("Send email", "ana@example.com", "Q3 report", context={"to": "ana@example.com", "subject": "Q3 report"}))
    cases.append(send('Email ana@example.com a message whose subject is "Q3 report"', "ana@example.com", "Q3 report"))
    for count, status in [(0, "missing_identifier"), (1, "valid_contract"), (2, "ambiguous_resource")]:
        selector = {"message_id": "new_msg_701"}
        item = valid('Check Gmail message with subject "Receipt 77" to ana@example.com is sent', "gmail", selector,
            [condition("gmail", selector, "location", "sent")], before={"subject": "Receipt 77"})
        item["resources"][0]["after"]["subject"] = "Receipt 77"
        item["resources"] *= count
        item["expected_status"] = status
        if count != 1:
            item["expected_conditions"] = []
        cases.append(item)
    cases.extend([
        invalid("gmail", "Send email", "missing_identifier"),
        invalid("gmail", "Send draft", "missing_identifier"),
        invalid("gmail", "Verify the customer read the email"),
        invalid("gmail", "Verify Gmail message msg101 was understood by the recipient"),
        invalid("gmail", "Verify Gmail message msg101 email body contains the full contract"),
        invalid("gmail", "Send email to ana@example.com with subject \"Q3\" exactly once"),
        invalid("gmail", "Send this through Outlook", "unsupported_provider"),
        invalid("gmail", "Check Exchange for the sent invoice", "unsupported_provider"),
        invalid("gmail", "Check Gmail message msg101 is sent", connection="disabled"),
        invalid("gmail", "Check Gmail message msg101 is sent", context={"refresh_token": "test-secret-sentinel"}),
        invalid("gmail", "Send email to ana@example.com with subject \"Report\" unless she opted out"),
    ])
    for source, event, obj, payload in [
        ("erp", "refund.completed", "order-42", ("payload.status", "refunded")),
        ("erp", "invoice.paid", "inv-482", ("payload.currency", "USD")),
        ("erp", "order.fulfilled", "order-55", None),
        ("warehouse", "shipment.dispatched", "ship-12", ("payload.carrier", "DHL")),
        ("warehouse", "shipment.delivered", "ship-18", None),
        ("billing", "subscription.cancelled", "sub-73", None),
        ("billing", "credit.issued", "credit-8", ("payload.status", "issued")),
        ("deploy", "deployment.completed", "deploy-95", ("payload.environment", "production")),
        ("deploy", "rollback.completed", "deploy-94", None),
        ("support", "ticket.resolved", "ticket-221", ("payload.resolution", "fixed")),
        ("support", "handoff.accepted", "ticket-222", None),
        ("crm", "renewal.signed", "renewal-14", None),
        ("crm", "lead.qualified", "lead-88", ("payload.segment", "enterprise")),
        ("hr", "onboarding.completed", "employee-91", None),
        ("hr", "access.revoked", "employee-15", None),
        ("payments", "payout.settled", "payout-31", ("payload.currency", "KES")),
        ("payments", "dispute.submitted", "dispute-6", None),
        ("signing", "document.signed", "doc-24", None),
        ("signing", "envelope.completed", "env-52", None),
        ("data", "export.completed", "export-19", ("payload.format", "csv")),
        ("data", "import.completed", "import-38", None),
        ("analytics", "report.ready", "report-77", None),
        ("security", "rotation.completed", "rotation-9", None),
        ("backup", "restore.completed", "restore-3", ("payload.status", "completed")),
        ("catalog", "product.published", "sku-128", None),
        ("catalog", "price.updated", "sku-129", ("payload.currency", "EUR")),
    ]:
        task = f'Wait for webhook "{event}" from "{source}" for object "{obj}"'
        if payload:
            task += f' with {payload[0]} = "{payload[1]}"'
        cases.append(hook(task, source, event, obj, payload))
    cases.append(hook('Once ERP emits refund.completed for order-42, verify that event through source "erp"', "erp", "refund.completed", "order-42"))
    cases.extend([
        invalid("webhook", "Confirm that the customer is happy after the refund"),
        invalid("webhook", "Verify the agent says the refund completed"),
        invalid("webhook", "Fetch an arbitrary URL and trust its success field", "unsupported_provider"),
        invalid("webhook", "Check https://example.com for a success message"),
        invalid("webhook", 'Wait for webhook "refund.completed" from "unknown-source" for object "order-42"', connection="unconfigured_source"),
        invalid("webhook", 'Wait for webhook "refund.completed" from "erp" for object "order-42"', connection="disabled"),
        invalid("webhook", 'Wait for webhook "refund.completed" from "erp" for object "order-42" with payload.access_token = "done"'),
        invalid("webhook", 'Wait for webhook "refund.completed" from "erp" for object "order-42"; Wait for webhook "refund.completed" from "erp" for object "order-42"'),
        invalid("webhook", 'Wait for webhook "refund.completed" from "erp" for object "order-42" with payload.status = "refunded"; Wait for webhook "refund.completed" from "erp" for object "order-42" with payload.status = "pending"'),
        invalid("webhook", "Verify a Slack reaction is positive", "unsupported_provider"),
        invalid("webhook", "Verify the report in Notion is correct", "unsupported_provider"),
        invalid("webhook", "Refund the most recent order"),
        invalid("webhook", "Wait for the ERP event without an object identifier"),
    ])
    for index, case in enumerate(cases, 1):
        case["id"] = f"task-{index:03}"
    return cases
