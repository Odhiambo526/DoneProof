from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__
from .adapters.base import ProviderAdapter
from .adapters.webhook import WebhookEvidenceAdapter
from .compiler import AstraCompiler
from .config import Settings, get_settings
from .connection_api import register_connection_routes
from .connections import ConnectionService, ManagedAdapter
from .demo import DEMO_HTML
from .domain import (
    CapabilityResponse,
    CompileRequest,
    CompletionContract,
    ProviderCapability,
    ReceiptIntegrity,
    RegisterRunRequest,
    VerificationReceipt,
    VerifyRequest,
    WebhookEventReceipt,
)
from .engine import VerificationEngine
from .limits import SlidingWindowLimiter
from .security import TenantContext, require_tenant
from .signing import ReceiptSigner
from .store import Store
from .web import CONSOLE_HTML, LANDING_HTML, certificate_html

VERSION = __version__

logger = logging.getLogger("doneproof.startup")


def create_app(
    settings: Settings | None = None, adapter_overrides: dict[str, ProviderAdapter] | None = None
) -> FastAPI:
    settings = settings or get_settings()
    if settings.verification_timeout_seconds <= 0:
        raise RuntimeError("DONEPROOF_VERIFICATION_TIMEOUT_SECONDS must be greater than zero")
    if settings.max_body_bytes < 1024:
        raise RuntimeError("DONEPROOF_MAX_BODY_BYTES must be at least 1024")
    if settings.max_batch_size < 1:
        raise RuntimeError("DONEPROOF_MAX_BATCH_SIZE must be at least 1")
    if settings.is_production and not settings.auth_enabled:
        raise RuntimeError("Production mode requires DONEPROOF_API_KEYS_JSON")
    if settings.is_production and not settings.has_stable_signing_key:
        raise RuntimeError("Production mode requires a stable DoneProof signing key")
    if settings.is_production and not settings.durable_storage:
        raise RuntimeError("Production mode requires durable PostgreSQL storage via DATABASE_URL")
    app = FastAPI(
        title="DoneProof API",
        version=VERSION,
        description="Independent outcome assurance and signed evidence receipts for AI agents.",
        contact={"name": "DoneProof"},
    )
    app.state.settings = settings
    app.state.store = Store(settings.storage_dsn)
    app.state.signer = ReceiptSigner(settings)
    app.state.compiler = AstraCompiler(settings)
    app.state.limiter = SlidingWindowLimiter(settings.requests_per_minute)
    app.state.connections = ConnectionService(app.state.store, settings)
    adapters = {
        "github": ManagedAdapter(app.state.connections, "github"),
        "gmail": ManagedAdapter(app.state.connections, "gmail"),
        "webhook": WebhookEvidenceAdapter(app.state.store),
    }
    if adapter_overrides:
        adapters.update(adapter_overrides)
    app.state.engine = VerificationEngine(adapters, app.state.signer, settings.verification_timeout_seconds)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=[
                "Content-Type",
                "X-DoneProof-Key",
                "X-DoneProof-Signature",
                "X-DoneProof-Timestamp",
                "X-DoneProof-Event",
                "X-DoneProof-Object-ID",
                "Idempotency-Key",
                "X-Request-ID",
            ],
        )

    @app.middleware("http")
    async def response_hardening(request: Request, call_next):
        supplied_request_id = request.headers.get("X-Request-ID") or ""
        request_id = (
            supplied_request_id
            if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", supplied_request_id)
            else f"req_{uuid.uuid4().hex[:16]}"
        )
        started = time.perf_counter()
        if (
            request.url.path.startswith("/v1/")
            and not request.url.path.startswith("/v1/webhooks/")
            and request.url.path != "/v1/signing-key"
        ):
            supplied = request.headers.get("X-DoneProof-Key") or ""
            if settings.auth_enabled:
                tenant_id = next(
                    (
                        tenant
                        for candidate, tenant in {**settings.api_keys, **settings.connection_admin_keys}.items()
                        if hmac.compare_digest(candidate, supplied)
                    ),
                    None,
                )
                limiter_key = f"tenant:{tenant_id}" if tenant_id else "unauthorized"
            else:
                limiter_key = "development"
            allowed, retry_after = app.state.limiter.check(limiter_key)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Workspace request rate exceeded"},
                    headers={"X-Request-ID": request_id, "Retry-After": str(retry_after)},
                )
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.max_body_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body exceeds configured limit"},
                        headers={"X-Request-ID": request_id},
                    )
            except ValueError:
                pass
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith(("/v1", "/connections")) else "public, max-age=300"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        )
        if request.url.path.startswith("/connections"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
        response.headers["X-DoneProof-Duration-Ms"] = f"{(time.perf_counter() - started) * 1000:.2f}"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def landing():
        return LANDING_HTML

    @app.get("/console", response_class=HTMLResponse, include_in_schema=False)
    def console():
        return CONSOLE_HTML

    @app.get("/demo", response_class=HTMLResponse, include_in_schema=False)
    def demo():
        return DEMO_HTML

    @app.get("/health", tags=["Operations"])
    def health():
        return {"ok": True, "service": "doneproof", "version": VERSION}

    @app.get("/ready", tags=["Operations"])
    def ready(request: Request):
        db_ok = request.app.state.store.ping()
        body = {
            "ready": db_ok,
            "database": "ready" if db_ok else "unavailable",
            "storage_backend": request.app.state.store.backend,
            "durable_storage": settings.durable_storage,
            "environment": settings.env,
            "warnings": [],
        }
        if not body["ready"]:
            return JSONResponse(status_code=503, content=body)
        return body

    @app.get("/v1/capabilities", response_model=CapabilityResponse, tags=["Workspace"])
    async def capabilities(ctx: TenantContext = Depends(require_tenant)):
        providers = [
            ProviderCapability(
                provider="github",
                status=app.state.connections.capability(ctx.tenant_id, "github"),
                description="Issues and pull requests with time-bounded resource discovery. Public anonymous reads are supported when no connection exists.",
            ),
            ProviderCapability(
                provider="gmail",
                status=app.state.connections.capability(ctx.tenant_id, "gmail"),
                description="Sent-vs-draft, recipients, subject, thread and attachment metadata.",
            ),
            ProviderCapability(
                provider="webhook",
                status="available"
                if any(x.tenant_id == ctx.tenant_id for x in settings.webhook_sources.values())
                else "configuration_required",
                description="Signed evidence events from customer systems and proprietary workflows.",
            ),
        ]
        return CapabilityResponse(
            version=VERSION,
            environment=settings.env,
            compiler="available" if settings.openai_api_key else "configuration_required",
            signing_key_id=app.state.signer.key_id,
            providers=providers,
        )

    @app.get("/v1/signing-key", tags=["Receipts"])
    async def signing_key():
        return {
            "algorithm": "Ed25519",
            "key_id": app.state.signer.key_id,
            "public_key": app.state.signer.public_key_b64,
            "trust_model": "Pin this public key through an independent channel before accepting receipt issuer authenticity.",
        }

    @app.post("/v1/contracts/compile", response_model=CompletionContract, tags=["Contracts"])
    async def compile_contract(req: CompileRequest, ctx: TenantContext = Depends(require_tenant)):
        try:
            contract = await app.state.compiler.compile(req.task, req.context, req.task_started_at)
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Contract compiler could not produce a valid completion contract.",
            ) from exc
        app.state.store.save_contract(ctx.tenant_id, contract)
        app.state.store.audit(
            ctx.tenant_id, "contract.compiled", "contract", contract.id, {"conditions": len(contract.postconditions)}
        )
        return contract

    @app.post("/v1/runs", response_model=CompletionContract, tags=["Runs"])
    async def register_run(req: RegisterRunRequest, ctx: TenantContext = Depends(require_tenant)):
        # High-assurance flow: the verifier, not the executor, establishes the
        # temporal boundary before the external action occurs.
        now = datetime.now(timezone.utc)
        contract = req.contract.model_copy(deep=True)
        contract.id = f"cc_{uuid.uuid4().hex[:16]}"
        contract.task_started_at = now
        contract.created_at = now
        app.state.store.save_contract(ctx.tenant_id, contract)
        baselines = await app.state.engine.snapshot(contract, ctx.tenant_id)
        for baseline in baselines:
            app.state.store.save_baseline(ctx.tenant_id, contract.id, baseline)
        app.state.store.audit(
            ctx.tenant_id,
            "run.registered",
            "contract",
            contract.id,
            {"conditions": len(contract.postconditions), "baselines": len(baselines)},
        )
        return contract

    @app.get("/v1/runs/{contract_id}", response_model=CompletionContract, tags=["Runs"])
    async def get_run(contract_id: str, ctx: TenantContext = Depends(require_tenant)):
        contract = app.state.store.get_contract(ctx.tenant_id, contract_id)
        if not contract:
            raise HTTPException(status_code=404, detail="Registered run not found")
        return contract

    @app.post("/v1/runs/{contract_id}/verify", response_model=VerificationReceipt, tags=["Runs"])
    async def verify_registered_run(
        contract_id: str,
        ctx: TenantContext = Depends(require_tenant),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        contract = app.state.store.get_contract(ctx.tenant_id, contract_id)
        if not contract:
            raise HTTPException(status_code=404, detail="Registered run not found")
        request_hash = hashlib.sha256(("registered:" + contract_id).encode()).hexdigest()
        if idempotency_key:
            if len(idempotency_key) > 200:
                raise HTTPException(status_code=400, detail="Idempotency-Key is too long")
            previous = app.state.store.get_idempotency(ctx.tenant_id, idempotency_key)
            if previous:
                if previous["request_hash"] != request_hash:
                    raise HTTPException(
                        status_code=409, detail="Idempotency-Key was already used for a different verification request"
                    )
                cached = app.state.store.get_receipt(ctx.tenant_id, previous["receipt_id"])
                if cached:
                    return cached
        baselines = app.state.store.get_baselines(ctx.tenant_id, contract_id)
        receipt = await app.state.engine.verify(
            contract, ctx.tenant_id, assurance_level="registered", baselines=baselines
        )
        app.state.store.save_receipt(ctx.tenant_id, receipt)
        app.state.store.audit(
            ctx.tenant_id,
            "run.verified",
            "receipt",
            receipt.receipt_id,
            {"contract_id": contract.id, "verdict": receipt.verdict.value, "assurance": receipt.assurance_level},
        )
        if idempotency_key:
            app.state.store.save_idempotency(ctx.tenant_id, idempotency_key, request_hash, receipt.receipt_id)
        return receipt

    @app.post("/v1/verify", response_model=VerificationReceipt, tags=["Verification"])
    async def verify(
        req: VerifyRequest,
        request: Request,
        ctx: TenantContext = Depends(require_tenant),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        raw_body = await request.body()
        try:
            canonical_request = json.dumps(json.loads(raw_body), sort_keys=True, separators=(",", ":")).encode()
        except json.JSONDecodeError:
            canonical_request = raw_body
        request_hash = hashlib.sha256(canonical_request).hexdigest()
        if idempotency_key:
            if len(idempotency_key) > 200:
                raise HTTPException(status_code=400, detail="Idempotency-Key is too long")
            previous = app.state.store.get_idempotency(ctx.tenant_id, idempotency_key)
            if previous:
                if previous["request_hash"] != request_hash:
                    raise HTTPException(
                        status_code=409, detail="Idempotency-Key was already used for a different verification request"
                    )
                cached = app.state.store.get_receipt(ctx.tenant_id, previous["receipt_id"])
                if cached:
                    return cached
        try:
            app.state.store.save_contract(ctx.tenant_id, req.contract)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        receipt = await app.state.engine.verify(req.contract, ctx.tenant_id)
        app.state.store.save_receipt(ctx.tenant_id, receipt)
        app.state.store.audit(
            ctx.tenant_id,
            "verification.completed",
            "receipt",
            receipt.receipt_id,
            {"contract_id": req.contract.id, "verdict": receipt.verdict.value, "assurance": receipt.assurance_level},
        )
        if idempotency_key:
            app.state.store.save_idempotency(ctx.tenant_id, idempotency_key, request_hash, receipt.receipt_id)
        return receipt

    @app.post("/v1/verify/batch", response_model=list[VerificationReceipt], tags=["Verification"])
    async def verify_batch(requests: list[VerifyRequest], ctx: TenantContext = Depends(require_tenant)):
        if not requests:
            raise HTTPException(status_code=400, detail="Batch must contain at least one verification request")
        if len(requests) > settings.max_batch_size:
            raise HTTPException(
                status_code=413, detail=f"Batch exceeds configured maximum of {settings.max_batch_size}"
            )
        try:
            for item in requests:
                app.state.store.save_contract(ctx.tenant_id, item.contract)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        receipts = list(
            await asyncio.gather(*(app.state.engine.verify(item.contract, ctx.tenant_id) for item in requests))
        )
        for receipt in receipts:
            app.state.store.save_receipt(ctx.tenant_id, receipt)
            app.state.store.audit(
                ctx.tenant_id,
                "verification.completed",
                "receipt",
                receipt.receipt_id,
                {
                    "contract_id": receipt.contract_id,
                    "verdict": receipt.verdict.value,
                    "assurance": receipt.assurance_level,
                    "batch": True,
                },
            )
        return receipts

    @app.get("/v1/receipts", response_model=list[VerificationReceipt], tags=["Receipts"])
    async def receipts(limit: int = Query(default=50, ge=1, le=200), ctx: TenantContext = Depends(require_tenant)):
        return app.state.store.list_receipts(ctx.tenant_id, limit)

    @app.get("/v1/receipts/{receipt_id}", response_model=VerificationReceipt, tags=["Receipts"])
    async def receipt(receipt_id: str, ctx: TenantContext = Depends(require_tenant)):
        item = app.state.store.get_receipt(ctx.tenant_id, receipt_id)
        if not item:
            raise HTTPException(status_code=404, detail="Receipt not found")
        return item

    @app.get("/v1/receipts/{receipt_id}/integrity", response_model=ReceiptIntegrity, tags=["Receipts"])
    async def receipt_integrity(receipt_id: str, ctx: TenantContext = Depends(require_tenant)):
        item = app.state.store.get_receipt(ctx.tenant_id, receipt_id)
        if not item:
            raise HTTPException(status_code=404, detail="Receipt not found")
        # This endpoint checks cryptographic integrity against the key carried
        # by the receipt. Issuer authenticity requires a separately pinned key.
        valid = ReceiptSigner.verify(item)
        return ReceiptIntegrity(
            receipt_id=item.receipt_id, valid=valid, key_id=item.key_id, receipt_hash=item.receipt_hash
        )

    @app.get("/v1/receipts/{receipt_id}/certificate", response_class=HTMLResponse, tags=["Receipts"])
    async def certificate(receipt_id: str, ctx: TenantContext = Depends(require_tenant)):
        item = app.state.store.get_receipt(ctx.tenant_id, receipt_id)
        if not item:
            raise HTTPException(status_code=404, detail="Receipt not found")
        return certificate_html(item)

    @app.get("/v1/receipts/{receipt_id}/bundle", tags=["Receipts"])
    async def receipt_bundle(receipt_id: str, ctx: TenantContext = Depends(require_tenant)):
        item = app.state.store.get_receipt(ctx.tenant_id, receipt_id)
        if not item:
            raise HTTPException(status_code=404, detail="Receipt not found")
        valid = ReceiptSigner.verify(item)
        return {
            "schema": "doneproof-evidence-bundle/v1",
            "receipt": item.model_dump(mode="json"),
            "integrity": {
                "valid": valid,
                "scope": "integrity_only",
                "receipt_hash": item.receipt_hash,
                "key_id": item.key_id,
            },
            "signing_key": {"algorithm": "Ed25519", "key_id": item.key_id, "public_key": item.public_key},
            "trust": {"issuer_authenticity": "requires_pinned_public_key"},
        }

    @app.get("/v1/audit", tags=["Workspace"])
    async def audit(limit: int = Query(default=100, ge=1, le=500), ctx: TenantContext = Depends(require_tenant)):
        return app.state.store.list_audit(ctx.tenant_id, limit)

    @app.get("/v1/overview", tags=["Workspace"])
    async def overview(ctx: TenantContext = Depends(require_tenant)):
        return app.state.store.stats(ctx.tenant_id)

    @app.post("/v1/webhooks/{source}", response_model=WebhookEventReceipt, tags=["Evidence"])
    async def ingest_webhook(
        source: str,
        request: Request,
        x_doneproof_signature: str | None = Header(default=None, alias="X-DoneProof-Signature"),
        x_doneproof_timestamp: str | None = Header(default=None, alias="X-DoneProof-Timestamp"),
        x_doneproof_event: str | None = Header(default=None, alias="X-DoneProof-Event"),
        x_doneproof_object_id: str | None = Header(default=None, alias="X-DoneProof-Object-ID"),
    ):
        source_cfg = settings.webhook_sources.get(source)
        if not source_cfg:
            raise HTTPException(status_code=404, detail="Evidence source not configured")
        if not x_doneproof_signature or not x_doneproof_timestamp or not x_doneproof_event:
            raise HTTPException(status_code=401, detail="Missing webhook authentication headers")
        try:
            ts = int(x_doneproof_timestamp)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid webhook timestamp") from exc
        if abs(int(time.time()) - ts) > settings.webhook_max_skew_seconds:
            raise HTTPException(status_code=401, detail="Webhook timestamp outside accepted replay window")
        raw = await request.body()
        if len(raw) > settings.max_body_bytes:
            raise HTTPException(status_code=413, detail="Webhook body exceeds configured limit")
        base = f"{ts}.{x_doneproof_event}.{x_doneproof_object_id or ''}.".encode() + raw
        expected = hmac.new(source_cfg.secret.encode(), base, hashlib.sha256).hexdigest()
        supplied = x_doneproof_signature.removeprefix("sha256=")
        if not hmac.compare_digest(expected, supplied):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Webhook body must be valid JSON") from exc
        occurred_at = datetime.fromtimestamp(ts, tz=timezone.utc)
        event_id = "evt_" + hashlib.sha256(source.encode() + b"\0" + base).hexdigest()[:28]
        inserted, _ = app.state.store.save_event(
            source_cfg.tenant_id,
            source,
            x_doneproof_event,
            x_doneproof_object_id,
            occurred_at,
            payload,
            event_id,
        )
        if inserted:
            app.state.store.audit(
                source_cfg.tenant_id,
                "evidence.accepted",
                "webhook_event",
                event_id,
                {"source": source, "event_type": x_doneproof_event, "object_id": x_doneproof_object_id},
            )
        return WebhookEventReceipt(
            event_id=event_id, accepted=True, duplicate=not inserted, source=source, occurred_at=occurred_at
        )

    register_connection_routes(app)
    return app


def _startup_error_code(exc: Exception) -> str:
    message = str(exc)
    if "DONEPROOF_API_KEYS_JSON" in message or "API_KEYS" in message:
        return "configuration.api_keys"
    if "signing key" in message.lower() or "DONEPROOF_SIGNING_SEED_B64" in message:
        return "configuration.signing_key"
    if "durable PostgreSQL" in message or "DATABASE_URL" in message:
        return "configuration.database_url"
    if exc.__class__.__module__.startswith("psycopg") or exc.__class__.__name__ in {
        "OperationalError",
        "DatabaseError",
    }:
        return "storage.unavailable"
    if isinstance(exc, RuntimeError):
        return "configuration.invalid"
    return "startup.unavailable"


def _create_startup_failure_app(error_code: str) -> FastAPI:
    """Return a fail-closed diagnostic app instead of crashing serverless import.

    No customer API is enabled in this state. The only useful endpoints are
    liveness/readiness diagnostics with a sanitized error class.
    """
    degraded = FastAPI(
        title="DoneProof API",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    environment = os.getenv("DONEPROOF_ENV", "development")

    @degraded.get("/health", include_in_schema=False)
    def degraded_health():
        return {"ok": False, "service": "doneproof", "version": VERSION, "startup": "degraded"}

    @degraded.get("/ready", include_in_schema=False)
    def degraded_ready():
        return JSONResponse(
            status_code=503,
            content={
                "ready": False,
                "database": "unknown",
                "storage_backend": "unknown",
                "durable_storage": False,
                "environment": environment,
                "warnings": [error_code],
            },
        )

    @degraded.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def degraded_root():
        return JSONResponse(
            status_code=503,
            content={
                "service": "doneproof",
                "status": "unavailable",
                "error": error_code,
                "check": "/ready",
            },
        )

    @degraded.api_route(
        "/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    def degraded_catch_all(path: str):
        return JSONResponse(
            status_code=503,
            content={"detail": "DoneProof is not ready", "error": error_code},
        )

    return degraded


def create_runtime_app() -> FastAPI:
    """Build the deployed ASGI app while keeping startup failures observable.

    `create_app()` remains strict and raises on unsafe production settings. This
    wrapper is only for the serverless module entrypoint: it converts startup
    failures into a fail-closed 503 app so `/ready` can identify the failing
    configuration class instead of Vercel reporting FUNCTION_INVOCATION_FAILED.
    """
    try:
        return create_app()
    except Exception as exc:  # pragma: no cover - exact provider errors vary
        error_code = _startup_error_code(exc)
        logger.error("DoneProof startup blocked: %s (%s)", error_code, exc.__class__.__name__)
        return _create_startup_failure_app(error_code)


app = create_runtime_app()
