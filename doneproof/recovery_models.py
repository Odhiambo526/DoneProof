"""Versioned, non-executable recovery information. No credentials or observations are accepted."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Remediation(BaseModel):
    kind: Literal["doneproof.remediation"] = "doneproof.remediation"
    condition: str
    status: Literal["FAIL", "UNKNOWN"]
    expected: Any = None
    observed: Any = None
    retryable: bool
    code: str
    action_hint: str
    reverify_after: Literal["external_action", "authoritative_evidence", "new_registered_run", "contract_revision"]


class RecoveryInfo(BaseModel):
    chain_id: str
    attempt: int = Field(default=0, ge=0, le=20)
    oscillating_conditions: list[str] = Field(default_factory=list)
    repeated_failures: list[str] = Field(default_factory=list)


class ReverifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    deadline_seconds: int = Field(default=300, ge=1, le=3600)
    callback_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class RecoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    automatic: bool
