from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from .domain import CompletionContract


# Structured Outputs stays narrow: the model defines verifiable outcomes and
# selectors, but it does not get an open-ended execution-plan schema.
CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "postconditions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                    "provider": {"type": "string", "enum": ["github", "unresolved"]},
                    "selector": {
                        "type": "object",
                        "properties": {
                            "repo": {"type": ["string", "null"]},
                            "kind": {"type": ["string", "null"], "enum": ["issue", "pull_request", None]},
                            "number": {"type": ["integer", "null"]},
                            "title": {"type": ["string", "null"]},
                            "author": {"type": ["string", "null"]},
                            "head_ref": {"type": ["string", "null"]},
                            "reason": {"type": ["string", "null"]},
                        },
                        "required": ["repo", "kind", "number", "title", "author", "head_ref", "reason"],
                        "additionalProperties": False,
                    },
                    "predicate": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": ["eq", "neq", "exists", "not_exists", "contains", "contains_all", "gte", "lte"],
                            },
                            "path": {"type": "string"},
                            "expected": {
                                "anyOf": [
                                    {"type": "string"},
                                    {"type": "number"},
                                    {"type": "boolean"},
                                    {"type": "null"},
                                    {"type": "array", "items": {"type": "string"}},
                                ]
                            },
                        },
                        "required": ["op", "path", "expected"],
                        "additionalProperties": False,
                    },
                    "required": {"type": "boolean"},
                },
                "required": ["id", "description", "provider", "selector", "predicate", "required"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["task", "assumptions", "postconditions"],
    "additionalProperties": False,
}

SYSTEM = """You compile human tasks into independently verifiable completion contracts.
The verifier currently supports GitHub issues and pull requests.

For every GitHub selector return all seven selector fields:
  repo: "owner/repo"
  kind: "issue" or "pull_request"
  number: positive integer if already known, otherwise null
  title: exact title if known, otherwise null
  author: GitHub login if known, otherwise null
  head_ref: exact head branch for a pull request if known, otherwise null
  reason: null

Normalized GitHub paths available:
number,title,body,state,locked,author,assignees,labels,created_at,updated_at,closed_at
and for pull requests: draft,merged,mergeable,head_ref,base_ref.

Rules:
- Describe OUTCOMES, never execution steps.
- Every required user outcome becomes a required postcondition.
- Prefer deterministic equality, membership, and existence predicates.
- Never invent a repo, issue/PR number, username, title, branch, or selector.
- A GitHub resource MAY be verifiable without its final number. If repo and kind are known and the intended new resource can be identified using an exact known title, author, or pull-request head_ref, return provider="github", number=null and include those known discovery constraints. DoneProof will independently search only resources created after task start.
- If identifiers are too weak to identify a resource safely, set provider="unresolved" and selector={repo:null,kind:null,number:null,title:null,author:null,head_ref:null,reason:"specific missing information"}. Give it a simple predicate such as {op:"exists",path:"",expected:null}. It will produce UNKNOWN rather than fake success.
- Use stable short ids p1, p2, p3.
- Keep the contract minimal and non-redundant.
"""


class AstraCompiler:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-6-astra")

    async def compile(
        self,
        task: str,
        context: dict[str, Any],
        task_started_at: datetime | None = None,
    ) -> CompletionContract:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        payload = {
            "model": self.model,
            "reasoning": {"effort": "low"},
            "input": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Task:\n{task}\n\nKnown context JSON:\n{json.dumps(context, ensure_ascii=False)}"},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "completion_contract",
                    "strict": True,
                    "schema": CONTRACT_SCHEMA,
                },
                "verbosity": "low",
            },
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=90.0) as client:
            r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        text = data.get("output_text") or self._extract_text(data)
        obj = json.loads(text)
        contract = CompletionContract.model_validate(obj)
        contract.task_started_at = (task_started_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self._validate_compiled_selectors(contract)
        return contract

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        for item in data.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") in {"output_text", "text"} and c.get("text"):
                        return c["text"]
        raise RuntimeError("OpenAI response did not contain output text")

    @staticmethod
    def _validate_compiled_selectors(contract: CompletionContract) -> None:
        for pc in contract.postconditions:
            if pc.provider != "github":
                continue
            repo = pc.selector.get("repo")
            kind = pc.selector.get("kind")
            number = pc.selector.get("number")
            if not repo or kind not in {"issue", "pull_request"}:
                raise ValueError(f"compiled GitHub selector is incomplete for {pc.id}")
            if number is not None:
                if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                    raise ValueError(f"compiled GitHub selector has invalid number for {pc.id}")
                continue
            title = pc.selector.get("title")
            author = pc.selector.get("author")
            head_ref = pc.selector.get("head_ref") if kind == "pull_request" else None
            if title is None and author is None and head_ref is None:
                raise ValueError(f"compiled GitHub discovery selector is too weak for {pc.id}")
