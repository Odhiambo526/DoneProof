from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _json(name: str, default: Any) -> Any:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must contain valid JSON") from exc


def _base64_secret(name: str) -> str | None:
    """Normalize common secret-manager copy/paste wrappers, then validate strictly later."""
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'", "`"}:
        value = value[1:-1].strip()
    value = "".join(value.split())
    return value or None


def _default_db_path() -> str:
    # Vercel Functions have an ephemeral writable /tmp filesystem. Keep the
    # normal local/container default everywhere else. A durable pilot should
    # configure persistent storage rather than rely on this fallback.
    if os.getenv("VERCEL") == "1":
        return "/tmp/doneproof.db"
    return "./doneproof.db"


@dataclass(frozen=True)
class WebhookSource:
    tenant_id: str
    secret: str


@dataclass(frozen=True)
class Settings:
    env: str
    db_path: str
    api_keys: dict[str, str]
    cors_origins: tuple[str, ...]
    verification_timeout_seconds: float
    openai_api_key: str | None
    openai_model: str
    github_token: str | None
    gmail_tokens: dict[str, str]
    gmail_access_token: str | None
    webhook_sources: dict[str, WebhookSource]
    webhook_max_skew_seconds: int
    signing_seed_b64: str | None
    legacy_receipt_key: str | None
    max_body_bytes: int
    requests_per_minute: int
    max_batch_size: int
    database_url: str | None = None
    connection_admin_keys: dict[str, str] = field(default_factory=dict, repr=False)
    connection_encryption_keys: dict[str, str] = field(default_factory=dict, repr=False)
    connection_active_key: str | None = None
    connection_public_url: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = field(default=None, repr=False)
    github_client_id: str | None = None
    github_client_secret: str | None = field(default=None, repr=False)
    github_app_slug: str | None = None
    legacy_connection_tenant: str | None = None
    job_callbacks: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict, repr=False)
    compiler_reasoning_effort: str = "low"
    max_reverification_attempts: int = 5
    browser_checks: dict = field(default_factory=dict, repr=False)

    @property
    def storage_dsn(self) -> str:
        return self.database_url or self.db_path

    @property
    def durable_storage(self) -> bool:
        return bool(
            self.database_url
            and (self.database_url.startswith("postgresql://") or self.database_url.startswith("postgres://"))
        )

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    def gmail_token_for(self, tenant_id: str) -> str | None:
        return self.gmail_tokens.get(tenant_id) or self.gmail_access_token

    @property
    def has_stable_signing_key(self) -> bool:
        if self.signing_seed_b64:
            try:
                return len(base64.b64decode(self.signing_seed_b64, validate=True)) == 32
            except (ValueError, TypeError, binascii.Error):
                return False
        return bool(self.legacy_receipt_key and self.legacy_receipt_key != "dev-only-change-me")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    api_keys_raw = _json("DONEPROOF_API_KEYS_JSON", {})
    if not isinstance(api_keys_raw, dict):
        raise RuntimeError("DONEPROOF_API_KEYS_JSON must be a JSON object mapping API keys to tenant ids")
    api_keys = {str(k): str(v) for k, v in api_keys_raw.items() if str(k) and str(v)}

    gmail_raw = _json("DONEPROOF_GMAIL_TOKENS_JSON", {})
    if not isinstance(gmail_raw, dict):
        raise RuntimeError("DONEPROOF_GMAIL_TOKENS_JSON must be a JSON object")
    gmail_tokens = {str(k): str(v) for k, v in gmail_raw.items() if str(k) and str(v)}

    webhook_raw = _json("DONEPROOF_WEBHOOK_SOURCES_JSON", {})
    if not isinstance(webhook_raw, dict):
        raise RuntimeError("DONEPROOF_WEBHOOK_SOURCES_JSON must be a JSON object")
    webhook_sources: dict[str, WebhookSource] = {}
    for source, spec in webhook_raw.items():
        if not isinstance(spec, dict) or not spec.get("secret"):
            raise RuntimeError(f"Webhook source {source!r} must define secret and optional tenant")
        webhook_sources[str(source)] = WebhookSource(
            tenant_id=str(spec.get("tenant") or "default"),
            secret=str(spec["secret"]),
        )

    cors = tuple(x.strip() for x in os.getenv("DONEPROOF_CORS_ORIGINS", "").split(",") if x.strip())
    return Settings(
        env=os.getenv("DONEPROOF_ENV", "development"),
        db_path=os.getenv("DONEPROOF_DB", _default_db_path()),
        api_keys=api_keys,
        cors_origins=cors,
        verification_timeout_seconds=float(os.getenv("DONEPROOF_VERIFICATION_TIMEOUT_SECONDS", "15")),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-6-astra"),
        github_token=os.getenv("GITHUB_TOKEN"),
        gmail_tokens=gmail_tokens,
        gmail_access_token=os.getenv("GMAIL_ACCESS_TOKEN"),
        webhook_sources=webhook_sources,
        webhook_max_skew_seconds=int(os.getenv("DONEPROOF_WEBHOOK_MAX_SKEW_SECONDS", "600")),
        signing_seed_b64=_base64_secret("DONEPROOF_SIGNING_SEED_B64"),
        legacy_receipt_key=os.getenv("DONEPROOF_RECEIPT_KEY"),
        max_body_bytes=int(os.getenv("DONEPROOF_MAX_BODY_BYTES", "1048576")),
        requests_per_minute=int(os.getenv("DONEPROOF_REQUESTS_PER_MINUTE", "120")),
        max_batch_size=int(os.getenv("DONEPROOF_MAX_BATCH_SIZE", "25")),
        database_url=os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL"),
        connection_admin_keys=_json("DONEPROOF_CONNECTION_ADMIN_KEYS_JSON", {}),
        connection_encryption_keys=_json("DONEPROOF_CONNECTION_ENCRYPTION_KEYS_JSON", {}),
        connection_active_key=os.getenv("DONEPROOF_CONNECTION_ACTIVE_KEY"),
        connection_public_url=os.getenv("DONEPROOF_PUBLIC_URL"),
        google_client_id=os.getenv("DONEPROOF_GOOGLE_CLIENT_ID"),
        google_client_secret=os.getenv("DONEPROOF_GOOGLE_CLIENT_SECRET"),
        github_client_id=os.getenv("DONEPROOF_GITHUB_CLIENT_ID"),
        github_client_secret=os.getenv("DONEPROOF_GITHUB_CLIENT_SECRET"),
        github_app_slug=os.getenv("DONEPROOF_GITHUB_APP_SLUG"),
        legacy_connection_tenant=os.getenv("DONEPROOF_LEGACY_CONNECTION_TENANT"),
        job_callbacks=_json("DONEPROOF_JOB_CALLBACKS_JSON", {}),
        max_reverification_attempts=int(os.getenv("DONEPROOF_MAX_REVERIFICATION_ATTEMPTS", "5")),
        compiler_reasoning_effort=os.getenv("DONEPROOF_COMPILER_REASONING_EFFORT", "low"),
        browser_checks=_json("DONEPROOF_BROWSER_CHECKS_JSON", {}),
    )
