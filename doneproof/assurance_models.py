"""Assurance protocol v1. Planning and lifecycle metadata are never evidence."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .browser_models import BrowserProvenance
from .compilation_models import CompilationResult
from .domain import CompletionContract, ConditionResult, Verdict, VerificationReceipt
from .recovery_models import Remediation, ReverifyRequest

SessionState = Literal['PREPARING', 'PREPARATION_FAILED', 'NEEDS_CLARIFICATION',
    'READY_FOR_EXECUTION', 'VERIFYING', 'VERIFIED', 'PARTIAL', 'FAILED', 'UNKNOWN',
    'REPAIR_PENDING', 'REVERIFYING']
JobState = Literal['QUEUED', 'OBSERVING', 'EVALUATING', 'SIGNING', 'COMPLETE',
                   'PARTIAL_FAILURE', 'EXPIRED', 'INTERNAL_ERROR']


class PrepareSession(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task: str = Field(min_length=3, max_length=4000)
    context: dict[str, JsonValue] = Field(default_factory=dict)
    require_transition: bool = Field(default=False, strict=True)


class AssuranceSummary(BaseModel):
    level: Literal['submitted', 'registered', 'transition_assured']
    required_conditions: int
    transition_required: int
    transitions_proven: int
    lower_assurance_browser: bool
    explanation: str


def assurance_summary(receipt):
    required = [r for r in receipt.results if r.required]
    transitions = [r for r in required if r.transition_required]
    proven = sum(r.status == 'PASS' and r.baseline_status == 'FAIL' for r in transitions)
    registered = receipt.assurance_level == 'registered'
    assured = registered and receipt.verdict == 'VERIFIED' and bool(transitions) and proven == len(transitions)
    return AssuranceSummary(level='transition_assured' if assured else receipt.assurance_level,
        required_conditions=len(required), transition_required=len(transitions),
        transitions_proven=proven if registered else 0,
        lower_assurance_browser=any(r.evidence.provenance for r in required),
        explanation=('The requested transitions passed against trusted pre-execution baselines.' if assured else
                     'A trusted boundary was registered; the receipt does not prove every requested transition.' if registered else
                     'Submitted contract: no trusted pre-execution transition assurance.'))


class VerifySession(ReverifyRequest):
    pass


class ReverifySession(ReverifyRequest):
    previous_receipt_id: str = Field(pattern=r'^vr_[a-f0-9]{20,32}$')


class CallbackDelivery(BaseModel):
    state: Literal['PENDING', 'SENDING', 'DELIVERED', 'DEAD']
    attempts: int
    error_code: str | None = None


class DurableJob(BaseModel):
    id: str
    state: JobState
    revision: int
    assurance_level: Literal['registered', 'submitted']
    condition_count: int
    created_at: float
    started_at: float | None
    finished_at: float | None
    deadline_at: float
    terminal_reason: str | None
    conditions: dict[str, int]
    receipt_id: str | None
    callback: CallbackDelivery | None = None


class ProviderBinding(BaseModel):
    provider: str
    version: str
    fingerprint: str


class EvidenceAssurance(BaseModel):
    condition: str
    provider: str
    evidence_class: Literal['provider_observation', 'browser_ui', 'unavailable']
    assurance_level: Literal['provider_declared', 'lower_than_authoritative_api', 'unavailable']
    provenance: BrowserProvenance | None = None


class ReceiptLink(BaseModel):
    receipt_id: str
    receipt_hash: str
    previous_receipt_id: str | None
    verdict: Verdict
    verified_at: datetime


class AssuranceSession(BaseModel):
    protocol_version: Literal['1.0'] = '1.0'
    id: str
    workspace: str
    task: str
    state: SessionState
    compiler: CompilationResult | None = None
    contract: CompletionContract | None = None
    trusted_task_started_at: datetime | None = None
    baselines: list[ConditionResult] = Field(default_factory=list)
    providers: list[ProviderBinding] = Field(default_factory=list)
    jobs: list[DurableJob] = Field(default_factory=list)
    current_job_id: str | None = None
    verdict: Verdict | None = None
    receipt: VerificationReceipt | None = None
    signed_payload_b64: str | None = None
    lineage: list[ReceiptLink] = Field(default_factory=list)
    remediation: list[Remediation] = Field(default_factory=list)
    can_reverify: bool = False
    evidence: list[EvidenceAssurance] = Field(default_factory=list)
    assurance: AssuranceSummary | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def needs_clarification(self) -> bool:
        return self.state == 'NEEDS_CLARIFICATION'

    @model_validator(mode='after')
    def coherent(self):
        if self.state == 'READY_FOR_EXECUTION' and (
                not self.contract or not self.trusted_task_started_at or not self.compiler
                or self.compiler.status != 'valid_contract' or self.compiler.clarification_requirements):
            raise ValueError('Invalid ready session')
        if self.state == 'NEEDS_CLARIFICATION' and (self.contract or not self.compiler
                or self.compiler.status == 'valid_contract' or not self.compiler.clarification_requirements):
            raise ValueError('Invalid clarification session')
        if self.verdict is not None and (not self.receipt or self.receipt.verdict != self.verdict):
            raise ValueError('Session verdict requires a matching receipt')
        if self.assurance is not None and (not self.receipt or self.assurance != assurance_summary(self.receipt)):
            raise ValueError('Assurance summary must match signed receipt facts')
        if self.receipt and any(r.evidence.provider == 'browser' and (
                self.receipt.schema_version != '1.2' or not r.evidence.provenance) for r in self.receipt.results):
            raise ValueError('Browser receipt provenance is required')
        for evidence in self.evidence:
            if (evidence.provider == 'browser' or evidence.provenance) and (
                    evidence.evidence_class != 'browser_ui' or evidence.assurance_level != 'lower_than_authoritative_api'
                    or not evidence.provenance):
                raise ValueError('Browser assurance mismatch')
        return self


class CompletionEvent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event_id: str = Field(pattern=r'^ve_[a-f0-9]{32}$')
    job_id: str = Field(pattern=r'^vj_[a-f0-9]{32}$')
    state: Literal['COMPLETE', 'PARTIAL_FAILURE', 'EXPIRED', 'INTERNAL_ERROR']
    receipt_id: str | None
    finished_at: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def bound_event(self):
        if self.event_id[3:] != self.job_id[3:]:
            raise ValueError('Event identity mismatch')
        expected = 'vr_' + self.job_id[3:] if self.state in {'COMPLETE', 'PARTIAL_FAILURE'} else None
        if self.receipt_id != expected:
            raise ValueError('Event receipt mismatch')
        return self
