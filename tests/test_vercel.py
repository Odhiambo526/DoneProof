from pathlib import Path
import tomllib

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
