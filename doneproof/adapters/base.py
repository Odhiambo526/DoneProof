from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ObservationContext:
    tenant_id: str
    contract_id: str
    task_started_at: str


@dataclass
class ProviderObservation:
    state: Any
    source_url: str | None = None
    note: str | None = None
    indeterminate: bool = False


class ProviderAdapter(ABC):
    @abstractmethod
    async def observe(self, selector: dict[str, Any], context: ObservationContext) -> ProviderObservation:
        raise NotImplementedError
