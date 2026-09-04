from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import Settings
from .domain import CompletionContract

_SELECTOR_PROPERTIES: dict[str, Any] = {
    "repo": {"type": ["string", "null"]},
    "kind": {"type": ["string", "null"], "enum": ["issue", "pull_request", None]},
    "number": {"type": ["integer", "null"]},
    "title": {"type": ["string", "null"]},
    "author": {"type": ["string", "null"]},
    "head_ref": {"type": ["string", "null"]},
    "message_id": {"type": ["string", "null"]},
    "subject": {"type": ["string", "null"]},
    "to": {"type": ["string", "null"]},
    "thread_id": {"type": ["string", "null"]},
    "location": {"type": ["string", "null"], "enum": ["sent", "draft", "other", None]},
    "source": {"type": ["string", "null"]},
    "event_type": {"type": ["string", "null"]},
    "object_id": {"type": ["string", "null"]},
    "reason": {"type": ["string", "null"]},
}

CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "postconditions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 30,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                    "provider": {"type": "string", "enum": ["github", "gmail", "webhook", "unresolved"]},
                    "selector": {
                        "type": "object",
                        "properties": _SELECTOR_PROPERTIES,
                        "required": list(_SELECTOR_PROPERTIES),
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
                    "require_change": {"type": "boolean"},
                },
                "required": ["id", "description", "provider", "selector", "predicate", "required", "require_change"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["task", "assumptions", "postconditions"],
    "additionalProperties": False,
}

SYSTEM = """You compile human tasks into independently verifiable completion contracts for DoneProof.
Supported evidence providers: GitHub, Gmail, and trusted webhook events.

Output OUTCOMES, never execution steps. Every required user outcome becomes a required postcondition. Keep contracts minimal and non-redundant. Never invent identifiers.

Return every selector field defined by the schema. Fields irrelevant to a provider MUST be null. reason MUST be null unless provider=unresolved.

GitHub normalized paths:
number,title,body,state,locked,author,assignees,labels,created_at,updated_at,closed_at,draft,merged,mergeable,head_ref,base_ref
GitHub selector: repo, kind, number if known; otherwise exact title/author/head_ref constraints for safe discovery.

Gmail normalized paths:
message_id,thread_id,location,subject,from,to,cc,bcc,internal_date,attachment_names
Gmail selector: message_id if known; otherwise subject/to/thread_id for discovery. Use location only if it is part of the requested outcome. For 'send email', location must be verified with predicate eq path='location' expected='sent'. Attachments use contains/contains_all on attachment_names.

Webhook normalized paths:
event_id,source,event_type,object_id,occurred_at,payload,payload_hash
Webhook selector: source,event_type,object_id if known. Predicates can inspect payload.<field>.

If identifiers are too weak to verify safely, use provider='unresolved', set every selector field null except reason, and use a simple exists predicate. UNKNOWN is preferable to fabricated assurance.
Use require_change=true when the user asks to mutate an existing resource and credit should require proof that the state changed during this run (for example assign, close, approve, update, merge). Use require_change=false for pure state assurance or new-resource discovery where the registered creation-time boundary already proves freshness.
Use stable ids p1,p2,p3. Prefer equality, membership and existence predicates.
"""


class AstraCompiler:
    def __init__(self, settings: Settings):
        self.api_key = settings.openai_api_key
        self.model = settings.openai_model

    async def compile(
        self, task: str, context: dict[str, Any], task_started_at: datetime | None = None
    ) -> CompletionContract:
        if not self.api_key:
            raise RuntimeError(
                "Contract compiler is not connected. Submit a completion contract directly or configure OPENAI_API_KEY."
            )
        payload = {
            "model": self.model,
            "reasoning": {"effort": "low"},
            "input": [
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": f"Task:\n{task}\n\nKnown context JSON:\n{json.dumps(context, ensure_ascii=False)}",
                },
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
        async with httpx.AsyncClient(timeout=90.0, follow_redirects=False) as client:
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
        raise RuntimeError("Model response did not contain contract output")

    @staticmethod
    def _validate_compiled_selectors(contract: CompletionContract) -> None:
        for pc in contract.postconditions:
            s = pc.selector
            if pc.provider == "github":
                if not s.get("repo") or s.get("kind") not in {"issue", "pull_request"}:
                    raise ValueError(f"compiled GitHub selector is incomplete for {pc.id}")
                number = s.get("number")
                if number is not None:
                    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                        raise ValueError(f"compiled GitHub selector has invalid number for {pc.id}")
                elif not any(
                    [s.get("title"), s.get("author"), s.get("head_ref") if s.get("kind") == "pull_request" else None]
                ):
                    raise ValueError(f"compiled GitHub discovery selector is too weak for {pc.id}")
            elif pc.provider == "gmail":
                if not s.get("message_id") and not any([s.get("subject"), s.get("to"), s.get("thread_id")]):
                    raise ValueError(f"compiled Gmail discovery selector is too weak for {pc.id}")
            elif pc.provider == "webhook":
                if not s.get("source") or not s.get("event_type"):
                    raise ValueError(f"compiled webhook selector is incomplete for {pc.id}")
