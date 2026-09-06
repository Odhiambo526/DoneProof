"""Fifty additional release tasks with independent expected outcomes.

Cross-provider cases combine reviewed independent outcomes, not compiler output.
Browser tasks intentionally lack approved server checks and must refuse/clarify.
Live browser success remains a separate staging gate.
"""
import copy

from evaluations.corpus import corpus, invalid


def rc_corpus():
    original = corpus()
    groups = {p: [x for x in original if x["provider"] == p and x["expected_status"] == "valid_contract"]
              for p in ("github", "gmail", "webhook")}
    cases = []
    for first, second in (("github", "gmail"), ("github", "webhook"), ("gmail", "webhook")):
        for i in range(10):
            a, b = copy.deepcopy(groups[first][i]), copy.deepcopy(groups[second][i])
            cases.append({**a, "task": a["task"] + "; " + b["task"], "provider": first + "+" + second,
                          "context": {**a["context"], **b["context"]},
                          "expected_conditions": a["expected_conditions"] + b["expected_conditions"],
                          "resources": a["resources"] + b["resources"]})
    for task in (
        "Close the issue and send the email", "Assign the requested owner to the PR",
        "Send the invoice to the right recipient", "Confirm the user read and understood the email",
        "Make the issue both open and closed", "Prove the agent's success claim is true",
        "Verify that the deployment is bug-free", "Confirm the refund made the customer happy",
        "Send the email but keep it exclusively in DRAFT", "Find any matching issue anywhere and approve it",
    ):
        cases.append(invalid("ambiguous", task))
    for task in (
        "Verify the billing portal shows paid using the executor screenshot",
        "Reuse the agent browser cookies to verify the order",
        "Verify browser check release-unapproved matches",
        "Verify the login page means the requested action succeeded",
        "Treat a CAPTCHA screen as proof of completion",
        "Use browser evidence when Gmail API authorization expires",
        "Use the GitHub website when its API rate limit is reached",
        "Verify the UI status that flashed briefly before disappearing",
        "Visit an arbitrary URL and certify the account state",
        "Use the executor-provided page HTML to prove delivery",
    ):
        cases.append(invalid("browser", task))
    for i, case in enumerate(cases):
        case["id"] = f"rc-{i + 1:03}"
    return cases
