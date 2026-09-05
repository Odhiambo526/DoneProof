from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..browser_models import BrowserProvenance


@dataclass(frozen=True)
class ObservationContext:
    tenant_id: str
    contract_id: str
    task_started_at: str
    condition_id: str = ""
    require_connection_binding: bool = False
    capture_connection_binding: bool = False


@dataclass
class ProviderObservation:
    state: Any
    source_url: str | None = None
    note: str | None = None
    indeterminate: bool = False
    authority: dict[str, Any] | None = None
    provenance: BrowserProvenance | None = None


class ProviderAdapter(ABC):
    def default_provenance(self):
        return None

    def validate_postcondition(self, pc):
        return True

    def observation_is_current(self, authority: dict[str, Any] | None, tenant_id: str) -> bool:
        return True

    @abstractmethod
    async def observe(self, selector: dict[str, Any], context: ObservationContext) -> ProviderObservation:
        raise NotImplementedError
