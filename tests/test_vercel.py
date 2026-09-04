import tomllib
from pathlib import Path

from doneproof.config import get_settings


def test_vercel_entrypoint_points_to_doneproof_app():
    config = tomllib.loads(Path("pyproject.toml").read_text())
    assert config["tool"]["vercel"]["entrypoint"] == "doneproof.app:app"


def test_vercel_uses_writable_tmp_sqlite_by_default(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("DONEPROOF_DB", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().db_path == "/tmp/doneproof.db"
    finally:
        get_settings.cache_clear()


def test_explicit_database_path_still_wins_on_vercel(monkeypatch, tmp_path):
    custom = str(tmp_path / "custom.db")
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("DONEPROOF_DB", custom)
    get_settings.cache_clear()
    try:
        assert get_settings().db_path == custom
    finally:
        get_settings.cache_clear()


def test_database_url_takes_precedence_over_vercel_tmp(monkeypatch):
    from doneproof.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db.example/doneproof")
    monkeypatch.delenv("DONEPROOF_DB", raising=False)
    settings = get_settings()
    assert settings.storage_dsn.startswith("postgresql://")
    assert settings.durable_storage is True
    get_settings.cache_clear()


def test_serverless_runtime_reports_missing_production_auth_as_503(monkeypatch):
    from fastapi.testclient import TestClient

    from doneproof.app import create_runtime_app

    monkeypatch.setenv("DONEPROOF_ENV", "production")
    monkeypatch.setenv("DONEPROOF_API_KEYS_JSON", "{}")
    monkeypatch.delenv("DONEPROOF_SIGNING_SEED_B64", raising=False)
    monkeypatch.delenv("DONEPROOF_RECEIPT_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    get_settings.cache_clear()
    try:
        client = TestClient(create_runtime_app())
        ready = client.get("/ready")
        assert ready.status_code == 503
        assert ready.json()["warnings"] == ["configuration.api_keys"]
        assert client.get("/").status_code == 503
    finally:
        get_settings.cache_clear()


def test_serverless_runtime_reports_missing_database_without_crashing(monkeypatch):
    import base64

    from fastapi.testclient import TestClient

    from doneproof.app import create_runtime_app

    monkeypatch.setenv("DONEPROOF_ENV", "production")
    monkeypatch.setenv("DONEPROOF_API_KEYS_JSON", '{"dp_live_test":"default"}')
    monkeypatch.setenv("DONEPROOF_SIGNING_SEED_B64", base64.b64encode(b"x" * 32).decode())
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    get_settings.cache_clear()
    try:
        ready = TestClient(create_runtime_app()).get("/ready")
        assert ready.status_code == 503
        assert ready.json()["warnings"] == ["configuration.database_url"]
    finally:
        get_settings.cache_clear()


def test_malformed_signing_seed_is_not_considered_stable(settings):
    from dataclasses import replace

    assert replace(settings, signing_seed_b64="!!!!").has_stable_signing_key is False
