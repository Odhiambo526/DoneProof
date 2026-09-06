from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field

from .browser_models import BrowserProvenance
from .security import _SENSITIVE_KEY, sanitize


class ObservationRecord(BaseModel):
    """Authoritative observation checkpoint; never accepted from an API caller."""
    state: Any = None
    source_url: str | None = None
    note: str | None = None
    indeterminate: bool = False
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    latency_ms: float = 0.0
    authority: dict[str, Any] | None = None
    provenance: BrowserProvenance | None = None
    redacted_paths: list[str] = Field(default_factory=list)

    def checkpoint(self):
        paths = set(self.redacted_paths)
        import copy
        state = copy.deepcopy(self.state)
        # Provider sensitivity can only add redaction, never relax global secret filtering.
        for path in paths:
            target = state
            parts = path.split(".")
            for part in parts[:-1]:
                target = target.get(part) if isinstance(target, dict) else None
            if isinstance(target, dict) and parts[-1] in target:
                target[parts[-1]] = "[redacted]"
        def walk(value, prefix=""):
            if isinstance(value, dict):
                for key, item in value.items():
                    path = f"{prefix}.{key}" if prefix else str(key)
                    if _SENSITIVE_KEY.search(str(key)):
                        paths.add(path)
                    else:
                        walk(item, path)
            elif isinstance(value, (list, tuple)):
                for index, item in enumerate(value):
                    walk(item, f"{prefix}.{index}" if prefix else str(index))
        walk(self.state)
        source = self.source_url
        if source:
            try:
                parts = urlsplit(source)
                source = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", "")) if parts.scheme in {"https", "http"} else None
            except ValueError:
                source = None
        return self.model_copy(update={"state": sanitize(state), "redacted_paths": sorted(paths), "source_url": source})

    def predicate_is_redacted(self, path):
        path = ".".join(part.strip() for part in path.split("."))
        return any(not path or path == redacted or path.startswith(redacted + ".") or redacted.startswith(path + ".")
                   for redacted in self.redacted_paths)
