"""Tenant administrator APIs and a browser-bound OAuth callback."""
from __future__ import annotations

import hmac
import re
from typing import Literal
from urllib.parse import parse_qs

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from .connection_web import CONNECTIONS_HTML, CONNECTIONS_JS
from .connections import ConnectionConflict


class ConnectionView(BaseModel):
    id: str
    provider: Literal["gmail", "github"]
    state: Literal["connected", "expired", "reconnect_required", "disabled", "error"]
    account_label: str | None
    expires_at: int | None
    refresh_expires_at: int | None
    last_checked_at: int | None
    error_code: str | None
    created_at: int
    updated_at: int
    scopes: list[str]
    revocation_pending: bool


class OnboardingProvider(BaseModel):
    provider: Literal["gmail", "github"]
    onboarding_available: bool
    installation_url: str | None


class ConnectionList(BaseModel):
    connections: list[ConnectionView]
    providers: list[OnboardingProvider]


class AuthorizationStart(BaseModel):
    authorization_url: str


class CallbackQueryPrivacy:
    """Remove OAuth query credentials before application/access logging sees the scope."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/v1/connections/oauth/"):
            raw = scope.get("query_string", b"")
            scope["query_string"] = b""
            scope["raw_path"] = scope["path"].encode("ascii")
            values = {}
            try:
                if len(raw) <= 32768:
                    parsed = parse_qs(raw.decode("ascii"), keep_blank_values=True, max_num_fields=12)
                    for key in ("state", "code", "error"):
                        parts = parsed.get(key, [])
                        if len(parts) == 1 and len(parts[0]) <= (16384 if key == "code" else 512):
                            values[key] = parts[0]
            except (UnicodeError, ValueError):
                pass
            scope["doneproof.oauth"] = values
        await self.app(scope, receive, send)


def register_connection_routes(app):
    service = app.state.connections
    settings = service.settings

    def administrator(request: Request):
        key = request.headers.get("X-DoneProof-Key", "")
        for candidate, tenant in settings.connection_admin_keys.items():
            if hmac.compare_digest(candidate, key):
                # Same-origin browser management; CLI requests have no Origin.
                origin = request.headers.get("origin")
                if origin and origin != (settings.connection_public_url or "").rstrip("/"):
                    raise HTTPException(403, "Connection management requires the configured origin")
                return tenant
        raise HTTPException(401, "A workspace connection administrator key is required")

    def provider_name(provider):
        if provider not in {"gmail", "github"}:
            raise HTTPException(404, "Provider not found")
        return provider

    def owned(tenant, connection_id):
        if not re.fullmatch(r"cn_[a-f0-9]{32}", connection_id):
            raise HTTPException(404, "Connection not found")
        row = service.db.get(tenant, connection_id=connection_id)
        if not row:
            raise HTTPException(404, "Connection not found")
        return row

    def cookie_name(provider):
        return ("__Host-" if settings.connection_public_url and
                settings.connection_public_url.startswith("https://") else "") + "doneproof_oauth_" + provider

    @app.get("/connections", response_class=HTMLResponse, include_in_schema=False)
    def connections_page():
        return HTMLResponse(CONNECTIONS_HTML, headers={"Cache-Control": "no-store"})

    @app.get("/connections.js", include_in_schema=False)
    def connections_script():
        return Response(CONNECTIONS_JS, media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/v1/connections", response_model=ConnectionList, tags=["Connections"])
    def list_connections(tenant: str = Depends(administrator)):
        return {"connections": [service.db.public(row) for row in service.db.list(tenant)],
                "providers": [{"provider": name, "onboarding_available": service.configured(name),
                    "installation_url": "https://github.com/apps/" + settings.github_app_slug + "/installations/new"
                        if name == "github" and settings.github_app_slug else None} for name in ("gmail", "github")]}

    @app.get("/v1/connections/{connection_id}", response_model=ConnectionView, tags=["Connections"])
    def get_connection(connection_id: str, tenant: str = Depends(administrator)):
        return service.db.public(owned(tenant, connection_id))

    @app.post("/v1/connections/{provider}/authorize", response_model=AuthorizationStart, tags=["Connections"])
    def authorize(provider: str, tenant: str = Depends(administrator)):
        provider_name(provider)
        if not service.configured(provider):
            raise HTTPException(503, "Connector onboarding is not configured")
        try:
            url, browser = service.start(tenant, provider)
        except ConnectionConflict:
            raise HTTPException(409, "Connection changed or revocation is pending; reload and retry") from None
        response = JSONResponse({"authorization_url": url})
        response.set_cookie(cookie_name(provider), browser, max_age=600, httponly=True,
                            secure=settings.connection_public_url.startswith("https://"), samesite="lax", path="/")
        return response

    @app.get("/v1/connections/oauth/{provider}/callback", include_in_schema=False)
    async def callback(provider: str, request: Request):
        provider_name(provider)
        values = request.scope.pop("doneproof.oauth", {})
        browser = request.cookies.get(cookie_name(provider), "")
        ok = False
        if values.get("state") and browser and service.configured(provider):
            ok = await service.callback(provider, values["state"], browser,
                                        values.get("code"), denied=bool(values.get("error")))
        response = RedirectResponse("/connections#connected" if ok else "/connections#authorization-failed", status_code=303)
        response.delete_cookie(cookie_name(provider), path="/",
                               secure=bool(settings.connection_public_url and settings.connection_public_url.startswith("https://")),
                               httponly=True, samesite="lax")
        return response

    @app.post("/v1/connections/{connection_id}/health", response_model=ConnectionView, tags=["Connections"])
    async def health(connection_id: str, tenant: str = Depends(administrator)):
        row = owned(tenant, connection_id)
        await service.usable(tenant, row["provider"], check_health=True)
        return service.db.public(owned(tenant, connection_id))

    @app.post("/v1/connections/{connection_id}/disconnect", response_model=ConnectionView, tags=["Connections"])
    async def disconnect(connection_id: str, tenant: str = Depends(administrator)):
        owned(tenant, connection_id)
        return service.db.public(await service.disconnect(tenant, connection_id))

    @app.post("/v1/connections/{connection_id}/rotate-key", response_model=ConnectionView, tags=["Connections"])
    def rotate_key(connection_id: str, tenant: str = Depends(administrator)):
        row = owned(tenant, connection_id)
        if row["lease_id"]:
            raise HTTPException(409, "Connection is busy")
        if row["credential_ciphertext"]:
            try:
                encrypted = service.vault.encrypt(row, service.vault.decrypt(row))
            except RuntimeError:
                raise HTTPException(503, "Connection encryption is unavailable") from None
            updated = service.db.update(row, credential_ciphertext=encrypted)
            if not updated:
                raise HTTPException(409, "Connection changed; reload and retry")
            service.db.audit(updated, "key_rotated")
            row = updated
        return service.db.public(row)

    @app.post("/v1/connections/{connection_id}/confirm-external-revocation", response_model=ConnectionView, tags=["Connections"])
    def confirm_external_revocation(connection_id: str, tenant: str = Depends(administrator)):
        row = owned(tenant, connection_id)
        updated = service.db.confirm_external_revocation(row)
        if not updated:
            raise HTTPException(409, "Disconnect first, then confirm revocation in provider settings")
        service.db.audit(updated, "external_revocation_confirmed")
        return service.db.public(updated)

    app.add_middleware(CallbackQueryPrivacy)
