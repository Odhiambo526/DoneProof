from fastapi import Depends

from .browser_policy import BrowserChecks
from .security import TenantContext, require_tenant


def register_browser_routes(app):
    @app.get("/v1/browser/checks", tags=["Workspace"])
    def checks(ctx: TenantContext = Depends(require_tenant)):
        installed = app.state.providers.get("browser") is not None
        listing = BrowserChecks(app.state.settings.browser_checks).listing(ctx.tenant_id) if installed else []
        return {"provider": "browser", "assurance": "lower_than_authoritative_api", "checks": listing}
