from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

from fastapi import Header, HTTPException, Request, status
from pydantic import BaseModel

from .config import Settings, get_settings

_SENSITIVE_KEY = re.compile(r"(token|secret|password|authorization|cookie|api[_-]?key|credential)", re.I)


class TenantContext(BaseModel):
    tenant_id: str
    api_key_fingerprint: str


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _SENSITIVE_KEY.search(str(key)):
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = sanitize(item)
        return out
    if isinstance(value, list):
        return [sanitize(x) for x in value]
    if isinstance(value, tuple):
        return [sanitize(x) for x in value]
    return value


async def require_tenant(
    request: Request,
    x_doneproof_key: str | None = Header(default=None, alias="X-DoneProof-Key"),
) -> TenantContext:
    settings: Settings = request.app.state.settings if hasattr(request.app.state, "settings") else get_settings()
    if not settings.auth_enabled:
        return TenantContext(tenant_id="default", api_key_fingerprint="development")
    if not x_doneproof_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-DoneProof-Key")
    for candidate, tenant_id in settings.api_keys.items():
        if hmac.compare_digest(candidate, x_doneproof_key):
            return TenantContext(tenant_id=tenant_id, api_key_fingerprint=fingerprint(x_doneproof_key))
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
