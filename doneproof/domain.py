from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class Verdict(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ConditionStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class Predicate(BaseModel):
    op: Literal[
        "eq",
        "neq",
        "exists",
        "not_exists",
        "contains",
        "contains_all",
        "gte",
        "lte",
    ]
    path: str = Field(description="Dot path into normalized provider state; blank means root.")
    expected: Any | None = None


class Postcondition(BaseModel):
    id: str
    description: str
    provider: Literal["github", "mock", "unresolved"]
    selector: dict[str, Any]
    predicate: Predicate
    required: bool = True


class CompletionContract(BaseModel):
    id: str = Field(default_factory=lambda: f"cc_{uuid4().hex[:16]}")
    task: str
    assumptions: list[str] = Field(default_factory=list)
    # Bound discovery to state changes that happened after the task began.
    # This prevents a pre-existing resource with the right title from being
    # accepted as evidence that the current agent run succeeded.
    task_started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    postconditions: list[Postcondition] = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [p.id for p in self.postconditions]
        if len(ids) != len(set(ids)):
            raise ValueError("postcondition ids must be unique")
        return self


class Evidence(BaseModel):
    provider: str
    selector: dict[str, Any]
    observed: Any | None = None
    source_url: str | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    note: str | None = None


class ConditionResult(BaseModel):
    id: str
    description: str
    required: bool
    status: ConditionStatus
    predicate: Predicate
    evidence: Evidence
    reason: str


class VerificationReceipt(BaseModel):
    receipt_id: str = Field(default_factory=lambda: f"vr_{uuid4().hex[:20]}")
    contract_id: str
    task: str
    verdict: Verdict
    results: list[ConditionResult]
    verified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    receipt_hash: str = ""
    signature: str = ""


class CompileRequest(BaseModel):
    task: str = Field(min_length=3)
    context: dict[str, Any] = Field(default_factory=dict)
    task_started_at: datetime | None = None


class VerifyRequest(BaseModel):
    contract: CompletionContract
