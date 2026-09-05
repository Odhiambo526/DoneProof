from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_serializer, model_validator

from .browser_models import BrowserProvenance
from .recovery_models import RecoveryInfo, Remediation

ProviderName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
AssuranceLevel = Literal["registered", "submitted"]


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
    id: str = Field(min_length=1, max_length=64, description="Stable condition identifier within the contract.")
    description: str = Field(
        min_length=2, max_length=300, description="Human-readable business outcome being verified."
    )
    provider: ProviderName = Field(description="Evidence provider used for this condition; browser UI carries lower assurance than an authoritative API.")
    selector: dict[str, Any] = Field(
        description="Provider-specific resource lookup constraints. Credentials must not be placed here."
    )
    predicate: Predicate = Field(description="Deterministic check evaluated against normalized provider state.")
    required: bool = Field(default=True, description="Required conditions determine the overall verdict.")
    require_change: bool = Field(
        default=False,
        description="Require a registered pre-execution false-to-true transition instead of state-only assurance.",
    )


class CompletionContract(BaseModel):
    schema_version: str = Field(default="1.0", description="Completion-contract schema version.")
    id: str = Field(
        default_factory=lambda: f"cc_{uuid4().hex[:16]}",
        description="Immutable contract identifier within a workspace.",
    )
    task: str = Field(min_length=3, max_length=4000, description="Requested business outcome in human-readable form.")
    assumptions: list[str] = Field(
        default_factory=list,
        max_length=30,
        description="Explicit assumptions used when translating intent into verifiable conditions.",
    )
    task_started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Verification time boundary. Registered runs replace caller input with server time.",
    )
    postconditions: list[Postcondition] = Field(
        min_length=1, max_length=50, description="Machine-checkable outcomes that define completion."
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), description="Contract creation timestamp."
    )

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
    provenance: BrowserProvenance | None = None

    @model_serializer(mode="wrap")
    def serialize_provenance(self, handler):
        data = handler(self)
        if self.provenance is None:
            # Do not change canonical bytes of already signed API/webhook receipts.
            data.pop("provenance", None)
        return data


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
    schema_version: str = Field(default="1.0", description="Verification-receipt schema version.")
    assurance_level: AssuranceLevel = Field(
        default="submitted", description="Whether DoneProof established the assurance boundary before execution."
    )
    receipt_id: str = Field(
        default_factory=lambda: f"vr_{uuid4().hex[:20]}", description="Unique verification receipt identifier."
    )
    contract_id: str = Field(description="Completion contract verified by this receipt.")
    contract_hash: str = Field(default="", description="SHA-256 of the exact canonical completion contract.")
    task: str = Field(description="Human-readable requested outcome copied from the contract.")
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
    remediation: list[Remediation] = Field(default_factory=list)
    previous_receipt_id: str | None = None
    previous_receipt_hash: str | None = None
    recovery: RecoveryInfo | None = None

    @model_validator(mode="after")
    def validate_recovery_version(self):
        if self.schema_version not in {"1.0", "1.1", "1.2"}:
            raise ValueError("Unsupported receipt schema")
        if self.schema_version != "1.2" and any(r.evidence.provenance for r in self.results):
            raise ValueError("Browser provenance requires receipt schema 1.2")
        if self.schema_version == "1.0" and (
                self.remediation or self.recovery or self.previous_receipt_id or self.previous_receipt_hash):
            raise ValueError("Recovery fields require receipt schema 1.1")
        if self.schema_version in {"1.1", "1.2"}:
            if self.recovery is None:
                raise ValueError("Missing recovery metadata")
            if bool(self.previous_receipt_id) != bool(self.previous_receipt_hash):
                raise ValueError("Incomplete receipt link")
            if bool(self.previous_receipt_id) != (self.recovery.attempt > 0):
                raise ValueError("Invalid recovery attempt")
            if not self.previous_receipt_id and self.recovery.chain_id != self.receipt_id:
                raise ValueError("Invalid root receipt")
        return self

    @model_serializer(mode="wrap")
    def serialize_version(self, handler):
        data = handler(self)
        if self.schema_version == "1.0":
            # Preserve the exact canonical payload of previously signed 1.0 receipts.
            for key in ("remediation", "previous_receipt_id", "previous_receipt_hash", "recovery"):
                data.pop(key, None)
        return data


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
    verification_scope: Literal["integrity_only"] = "integrity_only"


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
