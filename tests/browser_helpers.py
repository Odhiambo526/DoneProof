import base64
from dataclasses import replace

from fastapi.testclient import TestClient

from doneproof.adapters.browser import provider_definition
from doneproof.app import create_app
from doneproof.browser_policy import BrowserChecks
from doneproof.browser_runner import BrowserCapture
from doneproof.provider_registry import ProviderRegistry, default_registry
from doneproof.worker import VerificationWorker

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=")
CHECK = {"url": "https://status.example.org/releases/release-7", "page_marker": "#release-id",
         "page_text": "Release 7", "selector": "#release-status", "states": {"ready": "Complete", "pending": "Pending"},
         "success_state": "ready", "no_authoritative_api": True}


class Observer:
    def __init__(self):
        self.calls = []
        self.state = "ready"
        self.error = None

    async def capture(self, check):
        self.calls.append(check)
        if self.error:
            raise self.error
        return BrowserCapture(self.state, PNG)


def app_for(settings):
    configured = replace(settings, browser_checks={"tenant-a": {"release-7": CHECK}})
    registry = ProviderRegistry([*default_registry(), provider_definition()])
    app = create_app(configured, provider_registry=registry)
    observer = Observer()
    app.state.engine.adapters["browser"].observer = observer
    return app, TestClient(app, base_url="https://testserver"), VerificationWorker(app.state.store, app.state.engine), observer


def selector():
    return {"check_id": "release-7", "revision": BrowserChecks({"tenant-a": {"release-7": CHECK}}).get("tenant-a", "release-7").revision}


def payload(*, change=False):
    return {"contract": {"task": "Verify the approved UI check", "postconditions": [{"id": "p1",
        "provider": "browser", "description": "Release 7 UI status matches", "selector": selector(),
        "predicate": {"op": "eq", "path": "matched", "expected": True}, "require_change": change}]}}
