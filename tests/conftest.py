from __future__ import annotations

import base64
import os
import uuid
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
        requests_per_minute=120,
        max_batch_size=25,
    )


@pytest.fixture
def auth_settings(settings):
    return replace(settings, api_keys={"key-a": "tenant-a", "key-b": "tenant-b"})


@pytest.fixture
def webhook_settings(settings):
    return replace(settings, webhook_sources={"erp": WebhookSource(tenant_id="default", secret="whsec_test")})


@pytest.fixture(params=["sqlite", "postgresql"])
def connection_settings(request, auth_settings):
    configured = replace(auth_settings,
        connection_admin_keys={"admin-a": "tenant-a", "admin-b": "tenant-b"},
        connection_encryption_keys={"test-key": base64.b64encode(b"C" * 32).decode()},
        connection_active_key="test-key", connection_public_url="https://testserver",
        google_client_id="google-test-client", google_client_secret="google-secret-sentinel",
        github_client_id="github-test-client", github_client_secret="github-secret-sentinel",
        github_app_slug="doneproof-test")
    if request.param == "sqlite":
        yield configured
        return
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL not configured")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    schema = "connection_test_" + uuid.uuid4().hex
    dsn = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(dsn) as con:
        con.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    params = conninfo_to_dict(dsn)
    params["options"] = "-csearch_path=" + schema
    # Store intentionally accepts URL DSNs only; preserve that production contract.
    from urllib.parse import quote, urlencode
    scoped = ("postgresql://" + quote(params.get("user", "postgres"), safe="") + ":" +
              quote(params.get("password", ""), safe="") + "@" + params.get("host", "127.0.0.1") + ":" +
              params.get("port", "5432") + "/" + params.get("dbname", "postgres") + "?" +
              urlencode({k: v for k, v in params.items() if k not in {"user", "password", "host", "port", "dbname"}}))
    try:
        yield replace(configured, database_url=scoped)
    finally:
        with psycopg.connect(make_conninfo(dsn)) as con:
            con.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def connection_app(connection_settings):
    from doneproof.app import create_app
    from tests.connection_helpers import ProviderStub
    app = create_app(connection_settings)
    stub = ProviderStub()
    stub.attach(app.state.connections)
    return app, stub
