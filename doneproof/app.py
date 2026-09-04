from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from .adapters.github import GitHubAdapter
from .adapters.gmail import GmailAdapter
from .adapters.mock import MockAdapter
from .adapters.webhook import WebhookEvidenceAdapter
from .compiler import AstraCompiler
from .config import Settings, get_settings
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
from .security import TenantContext, require_tenant
from .signing import ReceiptSigner
from .store import Store
from .web import CONSOLE_HTML, LANDING_HTML, certificate_html

VERSION = "0.8.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="DoneProof API",
        version=VERSION,
        description="Independent outcome assurance and signed evidence receipts for AI agents.",
        contact={"name": "DoneProof"},
    )
    app.state.settings = settings
    app.state.store = Store(settings.db_path)
    app.state.signer = ReceiptSigner(settings)
    app.state.compiler = AstraCompiler(settings)
    adapters = {
        "github": GitHubAdapter(token=settings.github_token),
        "gmail": GmailAdapter(settings),
        "webhook": WebhookEvidenceAdapter(app.state.store),
    }
    if settings.enable_demo:
        adapters["mock"] = MockAdapter()
    app.state.engine = VerificationEngine(adapters, app.state.signer, settings.verification_timeout_seconds)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-DoneProof-Key", "X-DoneProof-Signature", "X-DoneProof-Timestamp", "X-DoneProof-Event", "X-DoneProof-Object-ID", "Idempotency-Key", "X-Request-ID"],
        )

    @app.middleware("http")
    async def response_hardening(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:16]}"
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.max_body_bytes:
                    return JSONResponse(status_code=413, content={"detail": "Request body exceeds configured limit"}, headers={"X-Request-ID": request_id})
            except ValueError:
                pass
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/v1") else "public, max-age=300"
        return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def landing():
        return LANDING_HTML

    @app.get("/console", response_class=HTMLResponse, include_in_schema=False)
    def console():
        return CONSOLE_HTML

    @app.get("/health", tags=["Operations"])
    def health():
        return {"ok": True, "service": "doneproof", "version": VERSION}

    @app.get("/ready", tags=["Operations"])
    def ready(request: Request):
        db_ok = request.app.state.store.ping()
        warnings: list[str] = []
        if settings.is_production and not settings.auth_enabled:
            warnings.append("API authentication is not configured")
        if settings.is_production and not settings.has_stable_signing_key:
            warnings.append("A stable production signing key is not configured")
        body = {
            "ready": db_ok and not warnings,
            "database": "ready" if db_ok else "unavailable",
            "environment": settings.env,
            "warnings": warnings,
        }
        if not body["ready"]:
            return JSONResponse(status_code=503, content=body)
        return body

    @app.get("/v1/capabilities", response_model=CapabilityResponse, tags=["Workspace"])
    async def capabilities(ctx: TenantContext = Depends(require_tenant)):
        gmail_available = bool(settings.gmail_token_for(ctx.tenant_id))
        providers = [
            ProviderCapability(provider="github", status="available", description="Issues and pull requests with time-bounded resource discovery."),
            ProviderCapability(provider="gmail", status="available" if gmail_available else "configuration_required", description="Sent-vs-draft, recipients, subject, thread and attachment metadata."),
            ProviderCapability(provider="webhook", status="available" if any(x.tenant_id == ctx.tenant_id for x in settings.webhook_sources.values()) else "configuration_required", description="Signed evidence events from customer systems and proprietary workflows."),
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
        return {"algorithm": "Ed25519", "key_id": app.state.signer.key_id, "public_key": app.state.signer.public_key_b64}

    @app.post("/v1/contracts/compile", response_model=CompletionContract, tags=["Contracts"])
    async def compile_contract(req: CompileRequest, ctx: TenantContext = Depends(require_tenant)):
        try:
            contract = await app.state.compiler.compile(req.task, req.context, req.task_started_at)
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Contract compiler could not produce a valid completion contract.") from exc
        app.state.store.save_contract(ctx.tenant_id, contract)
        return contract

    @app.post("/v1/runs", response_model=CompletionContract, tags=["Runs"])
    async def register_run(req: RegisterRunRequest, ctx: TenantContext = Depends(require_tenant)):
        now = datetime.now(timezone.utc)
        contract = req.contract.model_copy(deep=True)
        contract.id = f"cc_{uuid.uuid4().hex[:16]}"
        contract.task_started_at = now
        contract.created_at = now
        app.state.store.save_contract(ctx.tenant_id, contract)
        baselines = await app.state.engine.snapshot(contract, ctx.tenant_id)
        for baseline in baselines:
            app.state.store.save_baseline(ctx.tenant_id, contract.id, baseline)
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
                    raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different verification request")
                cached = app.state.store.get_receipt(ctx.tenant_id, previous["receipt_id"])
                if cached:
                    return cached
        baselines = app.state.store.get_baselines(ctx.tenant_id, contract_id)
        receipt = await app.state.engine.verify(contract, ctx.tenant_id, assurance_level="registered", baselines=baselines)
        app.state.store.save_receipt(ctx.tenant_id, receipt)
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
                    raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different verification request")
                cached = app.state.store.get_receipt(ctx.tenant_id, previous["receipt_id"])
                if cached:
                    return cached
        app.state.store.save_contract(ctx.tenant_id, req.contract)
        receipt = await app.state.engine.verify(req.contract, ctx.tenant_id)
        app.state.store.save_receipt(ctx.tenant_id, receipt)
        if idempotency_key:
            app.state.store.save_idempotency(ctx.tenant_id, idempotency_key, request_hash, receipt.receipt_id)
        return receipt

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
        valid = (
            item.key_id == app.state.signer.key_id
            and item.public_key == app.state.signer.public_key_b64
            and ReceiptSigner.verify(item)
        )
        return ReceiptIntegrity(receipt_id=item.receipt_id, valid=valid, key_id=item.key_id, receipt_hash=item.receipt_hash)

    @app.get("/v1/receipts/{receipt_id}/certificate", response_class=HTMLResponse, tags=["Receipts"])
    async def certificate(receipt_id: str, ctx: TenantContext = Depends(require_tenant)):
        item = app.state.store.get_receipt(ctx.tenant_id, receipt_id)
        if not item:
            raise HTTPException(status_code=404, detail="Receipt not found")
        return certificate_html(item)

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
        return WebhookEventReceipt(event_id=event_id, accepted=True, duplicate=not inserted, source=source, occurred_at=occurred_at)

    if settings.enable_demo:
        @app.post("/v1/demo/verify", response_model=VerificationReceipt, tags=["Demo"])
        async def verify_demo(ctx: TenantContext = Depends(require_tenant)):
            contract = CompletionContract.model_validate({
                "task": "Create GitHub issue 'Auth bypass' and assign alice",
                "postconditions": [
                    {"id":"p1","description":"Issue exists with requested title","provider":"mock","selector":{"state":{"title":"Auth bypass","assignees":[]}},"predicate":{"op":"eq","path":"title","expected":"Auth bypass"},"required":True},
                    {"id":"p2","description":"Alice is assigned","provider":"mock","selector":{"state":{"title":"Auth bypass","assignees":[]}},"predicate":{"op":"contains","path":"assignees","expected":"alice"},"required":True},
                ],
            })
            app.state.store.save_contract(ctx.tenant_id, contract)
            out = await app.state.engine.verify(contract, ctx.tenant_id, assurance_level="synthetic")
            app.state.store.save_receipt(ctx.tenant_id, out)
            return out

    return app


app = create_app()
