from __future__ import annotations

import copy
import json
import re
from typing import Any

import httpx

from .compilation_models import Candidate
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


CANDIDATE_SCHEMA = copy.deepcopy(CONTRACT_SCHEMA)
CANDIDATE_SCHEMA["properties"].pop("task")
CANDIDATE_SCHEMA["properties"].pop("assumptions")
CANDIDATE_SCHEMA["properties"]["intents"] = {
    "type": "array", "minItems": 1, "maxItems": 50,
    "items": {"type": "object", "additionalProperties": False,
              "properties": {"source_text": {"type": "string"},
                             "mode": {"type": "string", "enum": ["state", "transition", "create", "event", "unverifiable"]},
                             "condition_ids": {"type": "array", "items": {"type": "string"}}},
              "required": ["source_text", "mode", "condition_ids"]},
}
CANDIDATE_SCHEMA["properties"]["ambiguous"] = {"type": "boolean"}
CANDIDATE_SCHEMA["required"] = list(CANDIDATE_SCHEMA["properties"])

PIPELINE_SYSTEM = SYSTEM + """
Return a CANDIDATE for deterministic validation, not a certified contract.
First decompose the task into ordered intents. source_text must be verbatim task spans;
together they must cover the whole task, except punctuation and conjunctions.
Every condition belongs to exactly one intent. Never omit an unsupported outcome.
Use mode=unverifiable and provider=unresolved for it. ambiguous=true when intent has
multiple reasonable interpretations. A caller's claim of success is never evidence.
Only use the workspace capabilities supplied by the server. Credentials are unavailable.
Use only identifiers literally supplied in the task or typed context. Never invent IDs.
Existing-resource mutations require mode=transition and require_change=true on every condition.
New-resource creation uses mode=create; trusted future webhook events use mode=event.
All requested outcomes must be required. Do not use assumptions to remove requirements.
Use exact equality/collection predicates on supported fields, not root/field existence.
Gmail discovery requires BOTH exact subject and recipient; do not filter by location.
For send, include location=sent, subject equality and recipient containment.
GitHub discovery requires exact title; webhook selection requires source, event_type, object_id.
Conditions use p1, p2, ... identifiers. Do not include secrets, confidence or extra fields.
Read receipts, message body contents, PR review approvals, code correctness and business
satisfaction are not observable through these adapters. Do not substitute nearby metadata.
"""


class ModelUnavailable(Exception):
    pass


class InvalidCandidate(Exception):
    pass


class AstraCompiler:
    def __init__(self, settings: Settings):
        self.api_key = settings.openai_api_key
        self.model = settings.openai_model
        self.transport = None
        if self.model != "gpt-6-astra":
            raise ValueError("Completion Compiler v2 requires OPENAI_MODEL=gpt-6-astra")

    async def propose(self, task, context, capabilities, effort, feedback, usage) -> Candidate:
        if not self.api_key:
            raise ModelUnavailable() from None
        if effort not in {"low", "medium", "high", "xhigh"}:
            raise ValueError("Unsupported compilation reasoning effort")
        usage.model = self.model
        usage.efforts.append(effort)
        payload = {
            "model": self.model,
            "store": False,
            "max_output_tokens": 8192,
            "reasoning": {"effort": effort},
            "input": [
                {"role": "system", "content": PIPELINE_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps({"task": task, "context": context,
                        "workspace_capabilities": capabilities, "validation_feedback_codes": feedback}),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "completion_candidate",
                    "strict": True,
                    "schema": CANDIDATE_SCHEMA,
                },
                "verbosity": "low",
            },
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=40, follow_redirects=False, transport=self.transport) as client:
                r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError):
            usage.complete = False
            raise ModelUnavailable() from None
        if not isinstance(data, dict):
            usage.complete = False
            raise InvalidCandidate() from None
        raw_usage = data.get("usage")
        if not isinstance(raw_usage, dict):
            usage.complete = False
        else:
            details = raw_usage.get("input_tokens_details")
            output_details = raw_usage.get("output_tokens_details")
            details = details if isinstance(details, dict) else {}
            output_details = output_details if isinstance(output_details, dict) else {}
            for dest, value in {
                "input_tokens": raw_usage.get("input_tokens"),
                "output_tokens": raw_usage.get("output_tokens"),
                "cached_input_tokens": details.get("cached_tokens", 0),
                "cache_write_tokens": details.get("cache_write_tokens", 0),
                "reasoning_tokens": output_details.get("reasoning_tokens", 0),
            }.items():
                if type(value) is int and value >= 0:
                    setattr(usage, dest, getattr(usage, dest) + value)
                else:
                    usage.complete = False
        try:
            if data.get("status") != "completed":
                raise ValueError()
            candidate = Candidate.model_validate(json.loads(data.get("output_text") or self._extract_text(data)))
            ids = [pc.id for pc in candidate.postconditions] + [x for intent in candidate.intents for x in intent.condition_ids]
            if any(not re.fullmatch(r"p[1-9][0-9]{0,2}", ident) for ident in ids):
                raise ValueError()
            for pc in candidate.postconditions:
                pc.description = f"Requested {pc.provider} outcome"
            return candidate
        except (ValueError, TypeError, RuntimeError, KeyError):
            raise InvalidCandidate() from None

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
