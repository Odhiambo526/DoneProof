"""The asynchronous API has its own limits; the synchronous contract is unchanged."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domain import CompletionContract, Postcondition

TERMINAL = frozenset({"COMPLETE", "PARTIAL_FAILURE", "EXPIRED", "INTERNAL_ERROR"})


class JobContract(CompletionContract):
    postconditions: list[Postcondition] = Field(min_length=1, max_length=1000)


class CreateJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract: JobContract | None = None
    registered_contract_id: str | None = Field(default=None, min_length=1, max_length=200)
    deadline_seconds: int = Field(default=300, ge=1, le=3600)
    callback_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def one_contract(self):
        if (self.contract is None) == (self.registered_contract_id is None):
            raise ValueError("Provide exactly one contract or registered_contract_id")
        return self
