"""Non-evidence operational diagnostics; never imply remote worker liveness."""
import os
import re

from . import __version__
from .store import SCHEMA_VERSION


def deployment_revision():
    value = os.getenv("DONEPROOF_REVISION", "") or os.getenv("VERCEL_GIT_COMMIT_SHA", "")
    return value if re.fullmatch(r"[a-f0-9]{40}", value) else "unversioned"


def readiness(app):
    settings, store = app.state.settings, app.state.store
    try:
        db_ok = store.ping()
        version = store.schema_version() if db_ok else None
    except Exception:
        db_ok, version = False, None
    compatible = version == SCHEMA_VERSION
    browser = bool(settings.browser_checks)
    warnings = []
    if not compatible:
        warnings.append("schema_incompatible")
    if not settings.openai_api_key:
        warnings.append("compiler_model_not_configured")
    return {
        "ready": db_ok and compatible,
        "database": "ready" if db_ok else "unavailable",
        "storage_backend": store.backend, "durable_storage": settings.durable_storage,
        "environment": settings.env, "warnings": warnings,
        "scope": "api", "schema_version": version, "supported_schema_version": SCHEMA_VERSION,
        "signer": "configured", "signing_key_id": app.state.signer.key_id,
        "provider_registry": "validated_at_startup",
        "compiler": "configured_not_probed" if settings.openai_api_key else "deterministic_only",
        "browser": "configured_not_probed" if browser else "not_configured",
        "workers": "not_observed", "system_operational": None,
        "version": __version__, "revision": deployment_revision(),
    }
