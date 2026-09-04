from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
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
    enable_demo: bool
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
                return len(base64.b64decode(self.signing_seed_b64)) == 32
            except Exception:
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
        db_path=os.getenv("DONEPROOF_DB", "./doneproof.db"),
        api_keys=api_keys,
        cors_origins=cors,
        enable_demo=_bool("DONEPROOF_ENABLE_DEMO", default=False),
        verification_timeout_seconds=float(os.getenv("DONEPROOF_VERIFICATION_TIMEOUT_SECONDS", "15")),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-6-astra"),
        github_token=os.getenv("GITHUB_TOKEN"),
        gmail_tokens=gmail_tokens,
        gmail_access_token=os.getenv("GMAIL_ACCESS_TOKEN"),
        webhook_sources=webhook_sources,
        webhook_max_skew_seconds=int(os.getenv("DONEPROOF_WEBHOOK_MAX_SKEW_SECONDS", "600")),
        signing_seed_b64=os.getenv("DONEPROOF_SIGNING_SEED_B64"),
        legacy_receipt_key=os.getenv("DONEPROOF_RECEIPT_KEY"),
        max_body_bytes=int(os.getenv("DONEPROOF_MAX_BODY_BYTES", "1048576")),
    )
