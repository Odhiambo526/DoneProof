from jsonschema import Draft202012Validator
import pytest

from doneproof.compiler import AstraCompiler, CONTRACT_SCHEMA
from doneproof.domain import CompletionContract


def test_contract_schema_is_valid_json_schema():
    Draft202012Validator.check_schema(CONTRACT_SCHEMA)


def test_compiler_accepts_discovery_selector_without_number():
    c = CompletionContract.model_validate(
        {
            "task": "Create issue",
            "postconditions": [
                {
                    "id": "p1",
                    "description": "issue exists",
                    "provider": "github",
                    "selector": {
                        "repo": "acme/api",
                        "kind": "issue",
                        "number": None,
                        "title": "Auth bypass",
                        "author": None,
                        "head_ref": None,
                        "reason": None,
                    },
                    "predicate": {"op": "eq", "path": "title", "expected": "Auth bypass"},
                    "required": True,
                }
            ],
        }
    )
    AstraCompiler._validate_compiled_selectors(c)


def test_compiler_rejects_discovery_without_identity_constraints():
    c = CompletionContract.model_validate(
        {
            "task": "Create issue",
            "postconditions": [
                {
                    "id": "p1",
                    "description": "issue exists",
                    "provider": "github",
                    "selector": {
                        "repo": "acme/api",
                        "kind": "issue",
                        "number": None,
                        "title": None,
                        "author": None,
                        "head_ref": None,
                        "reason": None,
                    },
                    "predicate": {"op": "exists", "path": "", "expected": None},
                    "required": True,
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="too weak"):
        AstraCompiler._validate_compiled_selectors(c)
