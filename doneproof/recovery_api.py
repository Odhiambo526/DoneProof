"""Recovery requests carry scheduling options only; never execution claims or evidence."""
import asyncio
import json
import re

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .job_store import IdempotencyConflict, QueueFull, canonical, digest
from .recovery_models import RecoveryPolicy, ReverifyRequest
from .recovery_store import RecoveryError
from .remediation import remediation_for
from .security import TenantContext, require_tenant


async def parse(request, model, limit):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(413, "Request body exceeds configured limit")
    try:
        return model.model_validate(json.loads(body or b"{}"))
    except (ValueError, RecursionError):
        raise HTTPException(422, "Invalid recovery request") from None


def register_recovery_routes(app):
    db = app.state.recovery

    @app.exception_handler(RecoveryError)
    async def recovery_error(request, exc):
        return JSONResponse({"detail": exc.code}, status_code=exc.status, headers={"Cache-Control": "no-store"})

    @app.get("/v1/receipts/{receipt_id}/remediation", tags=["Recovery"])
    def remediation(receipt_id: str, ctx: TenantContext = Depends(require_tenant)):
        with db.transaction() as con:
            receipt = db.receipt(con, ctx.tenant_id, receipt_id)
        return {"receipt_id": receipt_id, "kind": "doneproof.remediation", "evidence": False,
                "remediation": receipt.remediation if receipt.schema_version == "1.1" else remediation_for(receipt.results)}

    @app.get("/v1/receipts/{receipt_id}/history", tags=["Recovery"])
    def history(receipt_id: str, ctx: TenantContext = Depends(require_tenant)):
        return db.history(ctx.tenant_id, receipt_id)

    @app.post("/v1/receipts/{receipt_id}/reverify", tags=["Recovery"], status_code=202,
              openapi_extra={"requestBody": {"content": {"application/json": {"schema": ReverifyRequest.model_json_schema()}}}})
    async def reverify(receipt_id: str, request: Request, ctx: TenantContext = Depends(require_tenant),
                       key: str | None = Header(default=None, alias="Idempotency-Key")):
        if not key or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", key):
            raise HTTPException(400, "A valid Idempotency-Key header is required")
        req = await parse(request, ReverifyRequest, app.state.settings.max_body_bytes)
        callback = None
        if req.callback_id:
            endpoint = app.state.job_callbacks.get(ctx.tenant_id, req.callback_id)
            if not endpoint:
                raise HTTPException(422, "Callback is not configured for this workspace")
            callback = req.callback_id, endpoint["fingerprint"]
        try:
            row, created = await asyncio.to_thread(db.reverify, ctx.tenant_id, receipt_id, key,
                digest(canonical({"receipt_id": receipt_id, **req.model_dump()})), req.deadline_seconds, callback)
        except IdempotencyConflict:
            raise HTTPException(409, "Idempotency key was used with a different request") from None
        except QueueFull:
            raise HTTPException(429, "Workspace verification queue is full", headers={"Retry-After": "30"}) from None
        return JSONResponse(await asyncio.to_thread(db.public, row), status_code=202 if created else 200,
                            headers={"Location": "/v1/jobs/" + row["id"]})

    @app.post("/v1/receipts/{receipt_id}/recovery-policy", tags=["Recovery"],
              openapi_extra={"requestBody": {"content": {"application/json": {"schema": RecoveryPolicy.model_json_schema()}}}})
    async def recovery_policy(receipt_id: str, request: Request, ctx: TenantContext = Depends(require_tenant)):
        req = await parse(request, RecoveryPolicy, app.state.settings.max_body_bytes)
        sources = {name for name, source in app.state.settings.webhook_sources.items() if source.tenant_id == ctx.tenant_id}
        await asyncio.to_thread(db.policy, ctx.tenant_id, receipt_id, req.automatic, sources)
        return await asyncio.to_thread(db.history, ctx.tenant_id, receipt_id)
