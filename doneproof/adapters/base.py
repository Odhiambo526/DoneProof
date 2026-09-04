from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ProviderObservation:
    state: Any
    source_url: str | None = None
    note: str | None = None
    # True means the provider returned evidence, but it is not safe to treat
    # that evidence as a single authoritative state (for example, discovery
    # matched multiple resources or GitHub returned a privacy-preserving 404).
    indeterminate: bool = False


class ProviderAdapter(ABC):
    @abstractmethod
    async def observe(self, selector: dict[str, Any]) -> ProviderObservation:
        raise NotImplementedError
