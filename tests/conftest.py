from __future__ import annotations

import base64
from dataclasses import replace

import pytest

from doneproof.config import Settings, WebhookSource


@pytest.fixture
def settings(tmp_path):
    return Settings(
        env="test",
        db_path=str(tmp_path / "doneproof.db"),
        api_keys={},
        cors_origins=(),
        enable_demo=True,
        verification_timeout_seconds=2.0,
        openai_api_key=None,
        openai_model="gpt-6-astra",
        github_token=None,
        gmail_tokens={},
        gmail_access_token=None,
        webhook_sources={},
        webhook_max_skew_seconds=600,
        signing_seed_b64=base64.b64encode(b"T" * 32).decode(),
        legacy_receipt_key=None,
        max_body_bytes=1048576,
    )


@pytest.fixture
def auth_settings(settings):
    return replace(settings, api_keys={"key-a": "tenant-a", "key-b": "tenant-b"})


@pytest.fixture
def webhook_settings(settings):
    return replace(settings, webhook_sources={"erp": WebhookSource(tenant_id="default", secret="whsec_test")})
