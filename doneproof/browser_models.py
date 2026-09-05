"""Browser provenance is issued by the verifier, never accepted as observation input."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ScreenshotRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str = Field(pattern=r"^bs_[a-f0-9]{32}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(gt=0, le=65536)
    media_type: Literal["image/png"] = "image/png"
    encrypted: Literal[True] = True
    expires_at: int


class BrowserProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["browser_ui"] = "browser_ui"
    assurance: Literal["lower_than_authoritative_api"] = "lower_than_authoritative_api"
    collector: Literal["doneproof.chromium.v1"] = "doneproof.chromium.v1"
    executor_supplied: Literal[False] = False
    fresh_context: Literal[True] | None = None
    authenticated: Literal[False] = False
    session_id: str | None = Field(default=None, pattern=r"^bo_[a-f0-9]{32}$")
    check_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    outcome: Literal["recognized", "unavailable", "not_configured", "invalid_condition", "policy_changed",
                     "api_required", "blocked_request", "login_or_challenge", "ambiguous_ui", "unstable_ui",
                     "screenshot_unavailable", "browser_unavailable", "deadline"] = "unavailable"
    recognized_state: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    samples: int = Field(default=0, ge=0, le=3)
    screenshot: ScreenshotRef | None = None
