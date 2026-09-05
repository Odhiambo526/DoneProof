"""Strict lifecycle inputs: task/context or scheduling options, never observations."""
import asyncio
import re

from fastapi import Depends, Header, HTTPException, Request

from .assurance import AssuranceService
from .assurance_models import AssuranceSession, PrepareSession, ReverifySession, VerifySession
from .contract_analysis import sensitive
from .domain import VerificationReceipt
from .job_api import contains_credentials
from .job_store import IdempotencyConflict, QueueFull
from .recovery_api import parse
from .security import TenantContext, require_tenant


def session_key(key):
    if not key or not re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', key):
        raise HTTPException(400, 'A valid Idempotency-Key header is required')
    return key


def register_assurance_routes(app):
    service = app.state.assurance = AssuranceService(app)

    @app.exception_handler(IdempotencyConflict)
    async def conflict(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': 'idempotency_conflict'}, status_code=409)

    @app.exception_handler(QueueFull)
    async def queue_full(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': 'workspace_queue_full'}, status_code=429, headers={'Retry-After': '30'})

    @app.post('/v1/assurance/sessions', response_model=AssuranceSession, tags=['Assurance sessions'],
              openapi_extra={'requestBody': {'required': True, 'content': {'application/json': {'schema': PrepareSession.model_json_schema(ref_template='#/components/schemas/{model}')}}}})
    async def prepare(request: Request, ctx: TenantContext = Depends(require_tenant),
                      key: str | None = Header(default=None, alias='Idempotency-Key')):
        session_key(key)
        req = await parse(request, PrepareSession, app.state.settings.max_body_bytes)
        allowed = {field for d in service.registry for field in d.manifest.context_fields}
        if set(req.context) - allowed or contains_credentials(req.context) or sensitive(req.task, req.context):
            raise HTTPException(422, 'Invalid assurance planning input')
        return await service.prepare(ctx.tenant_id, key, req)

    @app.get('/v1/assurance/sessions/{identifier}', response_model=AssuranceSession, tags=['Assurance sessions'])
    async def session(identifier: str, ctx: TenantContext = Depends(require_tenant)):
        return await asyncio.to_thread(service.view, ctx.tenant_id, identifier)

    async def schedule(identifier, request, ctx, key, reverify):
        session_key(key)
        req = await parse(request, ReverifySession if reverify else VerifySession, app.state.settings.max_body_bytes)
        await asyncio.to_thread(service.verify_session, ctx.tenant_id, identifier, key, req, reverify=reverify)
        return await asyncio.to_thread(service.view, ctx.tenant_id, identifier)

    @app.post('/v1/assurance/sessions/{identifier}/verify', response_model=AssuranceSession, status_code=202, tags=['Assurance sessions'],
              openapi_extra={'requestBody': {'content': {'application/json': {'schema': VerifySession.model_json_schema()}}}})
    async def verify(identifier: str, request: Request, ctx: TenantContext = Depends(require_tenant),
                     key: str | None = Header(default=None, alias='Idempotency-Key')):
        return await schedule(identifier, request, ctx, key, False)

    @app.post('/v1/assurance/sessions/{identifier}/reverify', response_model=AssuranceSession, status_code=202, tags=['Assurance sessions'],
              openapi_extra={'requestBody': {'content': {'application/json': {'schema': ReverifySession.model_json_schema()}}}})
    async def reverify(identifier: str, request: Request, ctx: TenantContext = Depends(require_tenant),
                       key: str | None = Header(default=None, alias='Idempotency-Key')):
        return await schedule(identifier, request, ctx, key, True)

    @app.get('/v1/assurance/sessions/{identifier}/receipt', response_model=VerificationReceipt, tags=['Assurance sessions'])
    async def receipt(identifier: str, ctx: TenantContext = Depends(require_tenant)):
        current = await asyncio.to_thread(service.view, ctx.tenant_id, identifier)
        if current.state in {'VERIFYING', 'REVERIFYING'} or not current.receipt:
            raise HTTPException(409, 'No completed receipt for the current attempt')
        return current.receipt
