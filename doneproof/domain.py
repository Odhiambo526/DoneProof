from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


ProviderName = Literal["github", "gmail", "webhook", "mock", "unresolved"]


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
    op: Literal["eq", "neq", "exists", "not_exists", "contains", "contains_all", "gte", "lte"]
    path: str = Field(description="Dot path into normalized provider state; blank means root.")
    expected: Any | None = None


class Postcondition(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=2, max_length=300)
    provider: ProviderName
    selector: dict[str, Any]
    predicate: Predicate
    required: bool = True
    require_change: bool = False


class CompletionContract(BaseModel):
    schema_version: str = "1.0"
    id: str = Field(default_factory=lambda: f"cc_{uuid4().hex[:16]}")
    task: str = Field(min_length=3, max_length=4000)
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    task_started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    postconditions: list[Postcondition] = Field(min_length=1, max_length=50)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_contract(self):
        ids = [p.id for p in self.postconditions]
        if len(ids) != len(set(ids)):
            raise ValueError("postcondition ids must be unique")
        if not any(p.required for p in self.postconditions):
            raise ValueError("at least one postcondition must be required")
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
    transition_required: bool = False
    baseline_status: ConditionStatus | None = None
    baseline_observed: Any | None = None
    latency_ms: float = 0.0


class VerificationSummary(BaseModel):
    total: int
    required: int
    passed: int
    failed: int
    unknown: int
    providers: list[str]


class VerificationReceipt(BaseModel):
    schema_version: str = "1.0"
    assurance_level: Literal["registered", "submitted", "synthetic"] = "submitted"
    receipt_id: str = Field(default_factory=lambda: f"vr_{uuid4().hex[:20]}")
    contract_id: str
    task: str
    verdict: Verdict
    summary: VerificationSummary
    results: list[ConditionResult]
    verified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: float = 0.0
    receipt_hash: str = ""
    signature_alg: str = "Ed25519"
    key_id: str = ""
    public_key: str = ""
    signature: str = ""


class CompileRequest(BaseModel):
    task: str = Field(min_length=3, max_length=4000)
    context: dict[str, Any] = Field(default_factory=dict)
    task_started_at: datetime | None = None


class VerifyRequest(BaseModel):
    contract: CompletionContract


class RegisterRunRequest(BaseModel):
    contract: CompletionContract


class ReceiptIntegrity(BaseModel):
    receipt_id: str
    valid: bool
    key_id: str
    receipt_hash: str


class ProviderCapability(BaseModel):
    provider: str
    status: Literal["available", "configuration_required", "disabled"]
    description: str


class CapabilityResponse(BaseModel):
    version: str
    environment: str
    compiler: Literal["available", "configuration_required"]
    signing_key_id: str
    providers: list[ProviderCapability]


class WebhookEventReceipt(BaseModel):
    event_id: str
    accepted: bool
    duplicate: bool
    source: str
    occurred_at: datetime
