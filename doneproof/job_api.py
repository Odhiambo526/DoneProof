from __future__ import annotations

import asyncio
import json
import re
import time

from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .job_models import TERMINAL, CreateJob
from .job_store import IdempotencyConflict, QueueFull, canonical, digest
from .security import _SENSITIVE_KEY, TenantContext, require_tenant


def request_schema():
    schema = CreateJob.model_json_schema()
    definitions = schema.pop("$defs", {})
    def resolve(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return resolve(definitions[value["$ref"].split("/")[-1]])
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value
    return resolve(schema)


def contains_credentials(value):
    if isinstance(value, dict):
        return any(_SENSITIVE_KEY.search(str(key)) or contains_credentials(item) for key, item in value.items())
    return any(contains_credentials(item) for item in value) if isinstance(value, list) else False


def register_job_routes(app):
    db = app.state.jobs

    def owned(tenant, job_id):
        row = db.get_job(tenant, job_id)
        if not row:
            raise HTTPException(404, "Verification job not found")
        return row

    @app.post("/v1/jobs", tags=["Verification jobs"], status_code=202,
              openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": request_schema()}}}})
    async def create_job(request: Request, ctx: TenantContext = Depends(require_tenant),
                         key: str | None = Header(default=None, alias="Idempotency-Key")):
        if not key or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", key):
            raise HTTPException(400, "A valid Idempotency-Key header is required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > app.state.settings.max_body_bytes:
                raise HTTPException(413, "Request body exceeds configured limit")
        try:
            raw = json.loads(body)
            req = CreateJob.model_validate(raw)
            if contains_credentials(raw):
                raise ValueError("Credentials belong in managed connections")
        except (ValueError, ValidationError, RecursionError):
            # Do not echo arbitrary request fields (including accidental secrets) in validation errors.
            raise HTTPException(422, "Invalid verification job request") from None
        request_hash = digest(canonical(raw))
        try:
            existing = await asyncio.to_thread(db.idempotent_job, ctx.tenant_id, key, request_hash)
        except IdempotencyConflict:
            raise HTTPException(409, "Idempotency key was used with a different request") from None
        if existing:
            result = await asyncio.to_thread(lambda: db.public(owned(ctx.tenant_id, existing["id"])))
            return JSONResponse(result, status_code=200,
                                headers={"Location": "/v1/jobs/" + existing["id"]})
        callback = None
        if req.callback_id:
            endpoint = app.state.job_callbacks.get(ctx.tenant_id, req.callback_id)
            if not endpoint:
                raise HTTPException(422, "Callback is not configured for this workspace")
            callback = req.callback_id, endpoint["fingerprint"]
        contract, baselines, assurance = req.contract, {}, "submitted"
        if req.registered_contract_id:
            contract = await asyncio.to_thread(app.state.store.get_contract, ctx.tenant_id, req.registered_contract_id)
            if not contract or not await asyncio.to_thread(db.registered, ctx.tenant_id, req.registered_contract_id):
                raise HTTPException(404, "Registered contract not found")
            # A completed /v1/runs registration owns the temporal boundary and captured baselines.
            baselines = await asyncio.to_thread(app.state.store.get_baselines, ctx.tenant_id, req.registered_contract_id)
            assurance = "registered"
        if not app.state.providers.accepts(contract):
            raise HTTPException(422, "Contract contains a provider not installed in this deployment")
        try:
            row, created = await asyncio.to_thread(db.create, ctx.tenant_id, key, request_hash, contract, baselines,
                                                   assurance, req.deadline_seconds, callback)
        except IdempotencyConflict:
            raise HTTPException(409, "Idempotency key was used with a different request") from None
        except QueueFull:
            raise HTTPException(429, "Workspace verification queue is full", headers={"Retry-After": "30"}) from None
        return JSONResponse(await asyncio.to_thread(db.public, row), status_code=202 if created else 200,
                            headers={"Location": "/v1/jobs/" + row["id"]})

    @app.get("/v1/jobs/{job_id}", tags=["Verification jobs"])
    def job_status(job_id: str, ctx: TenantContext = Depends(require_tenant)):
        return db.public(owned(ctx.tenant_id, job_id))

    @app.get("/v1/jobs/{job_id}/conditions", tags=["Verification jobs"])
    def job_conditions(job_id: str, ctx: TenantContext = Depends(require_tenant),
                       offset: int = Query(default=0, ge=0, le=1000), limit: int = Query(default=100, ge=1, le=1000)):
        owned(ctx.tenant_id, job_id)
        return {"conditions": db.conditions(ctx.tenant_id, job_id, public=True, offset=offset, limit=limit)}

    @app.post("/v1/jobs/{job_id}/cancel", tags=["Verification jobs"])
    def cancel_job(job_id: str, ctx: TenantContext = Depends(require_tenant)):
        row = db.cancel(ctx.tenant_id, job_id)
        if not row:
            raise HTTPException(404, "Verification job not found")
        return db.public(row)

    @app.get("/v1/jobs/{job_id}/wait", tags=["Verification jobs"])
    async def wait_job(job_id: str, request: Request, ctx: TenantContext = Depends(require_tenant),
                       after_revision: int = Query(default=-1, ge=-1), timeout: float = Query(default=20, ge=0, le=25)):
        until = time.monotonic() + timeout
        while True:
            row = await asyncio.to_thread(owned, ctx.tenant_id, job_id)
            if row["state"] in TERMINAL or row["revision"] > after_revision or time.monotonic() >= until:
                return await asyncio.to_thread(db.public, row)
            if await request.is_disconnected():
                return JSONResponse(status_code=204, content=None)
            await asyncio.sleep(min(0.25, max(0, until - time.monotonic())))
