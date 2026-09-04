from jsonschema import Draft202012Validator
import pytest

from doneproof.compiler import AstraCompiler, CONTRACT_SCHEMA
from doneproof.domain import CompletionContract


def pc(provider, selector):
    return {"id":"p1","description":"outcome exists","provider":provider,"selector":selector,"predicate":{"op":"exists","path":"","expected":None},"required":True}


def test_contract_schema_is_valid_json_schema():
    Draft202012Validator.check_schema(CONTRACT_SCHEMA)


def test_compiler_accepts_github_discovery_without_number():
    c = CompletionContract.model_validate({"task":"Create issue","postconditions":[pc("github", {"repo":"acme/api","kind":"issue","number":None,"title":"Auth bypass"})]})
    AstraCompiler._validate_compiled_selectors(c)


def test_compiler_rejects_weak_github_discovery():
    c = CompletionContract.model_validate({"task":"Create issue","postconditions":[pc("github", {"repo":"acme/api","kind":"issue","number":None})]})
    with pytest.raises(ValueError, match="too weak"):
        AstraCompiler._validate_compiled_selectors(c)


def test_compiler_accepts_gmail_and_webhook_selectors():
    gmail = CompletionContract.model_validate({"task":"Send mail","postconditions":[pc("gmail", {"subject":"Invoice","to":"a@example.com"})]})
    hook = CompletionContract.model_validate({"task":"Refund","postconditions":[pc("webhook", {"source":"erp","event_type":"refund.completed","object_id":"r1"})]})
    AstraCompiler._validate_compiled_selectors(gmail)
    AstraCompiler._validate_compiled_selectors(hook)
