"""Operator-owned, exact UI probes. No endpoint accepts URLs or browser state."""
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ID = r"^[a-z][a-z0-9_-]{0,63}$"
REVISION = r"^[a-f0-9]{64}$"
# Provider-owned denylist for the currently supported first-party API surfaces.
# Other API coverage must also be declared by the operator, including private APIs.
API_HOSTS = ("github.com", "githubusercontent.com", "github.dev", "gmail.com", "mail.google.com",
             "googleapis.com")


def public_url(value):
    try:
        p = urlsplit(value)
        if (p.scheme != "https" or not p.hostname or p.username or p.password or p.query or p.fragment
                or p.port not in (None, 443) or len(value) > 512 or not p.path.startswith("/")
                or not re.fullmatch(r"https://[a-z0-9.-]+/[A-Za-z0-9/_.~-]*", value)
                or any(part in {".", ".."} for part in p.path.split("/"))
                or p.hostname.endswith((".local", ".localhost", ".internal", ".test"))):
            raise ValueError
        try:
            address = ipaddress.ip_address(p.hostname)
        except ValueError:
            if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", p.hostname):
                raise ValueError from None
        else:
            if not address.is_global:
                raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Browser destinations require an exact public HTTPS URL without credentials or parameters") from None
    return value


class BrowserCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    url: str
    # Exact resources, not origins or patterns. JavaScript/XHR is optional and GET-only.
    resources: tuple[str, ...] = Field(default=(), max_length=24)
    page_marker: str = Field(pattern=r"^#[A-Za-z][A-Za-z0-9_-]{0,63}$")
    page_text: str = Field(min_length=1, max_length=120)
    selector: str = Field(pattern=r"^#[A-Za-z][A-Za-z0-9_-]{0,63}$")
    states: dict[str, str] = Field(min_length=2, max_length=8)
    success_state: str
    # Required human coverage decision; an unavailable connection is not "no API".
    no_authoritative_api: bool
    authoritative_provider: str | None = Field(default=None, max_length=64)
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def destination(cls, v):
        return public_url(v)

    @field_validator("resources", mode="before")
    @classmethod
    def resources_tuple(cls, v):
        if isinstance(v, list):
            return tuple(v)
        return v

    @model_validator(mode="after")
    def bounded(self):
        for url in self.resources:
            public_url(url)
        if (self.selector == self.page_marker or self.success_state not in self.states
                or len(set(self.states.values())) != len(self.states)
                or any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", k) or not isinstance(v, str)
                       or not 1 <= len(v) <= 120 or v != v.strip() for k, v in self.states.items())
                or self.page_text != self.page_text.strip()):
            raise ValueError("Browser checks require distinct, bounded, recognizable states and a separate page marker")
        return self

    @property
    def api_required(self):
        return (not self.no_authoritative_api or bool(self.authoritative_provider)
                or any(urlsplit(u).hostname == h or urlsplit(u).hostname.endswith("." + h)
                       for u in (self.url, *self.resources) for h in API_HOSTS))

    @property
    def revision(self):
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True,
                                        separators=(",", ":")).encode()).hexdigest()


class BrowserChecks:
    def __init__(self, raw):
        self._checks = {}
        try:
            if not isinstance(raw, dict) or len(raw) > 1000:
                raise ValueError
            for tenant, checks in raw.items():
                if not isinstance(tenant, str) or not 1 <= len(tenant) <= 128 or not isinstance(checks, dict) or len(checks) > 100:
                    raise ValueError
                self._checks[tenant] = {}
                for identifier, spec in checks.items():
                    if not isinstance(identifier, str) or not re.fullmatch(ID, identifier):
                        raise ValueError
                    self._checks[tenant][identifier] = BrowserCheck.model_validate(spec)
        except (ValueError, TypeError):
            # Validation must not echo operator configuration or page literals.
            raise RuntimeError("Invalid browser check configuration") from None

    def get(self, tenant, identifier):
        check = self._checks.get(tenant, {}).get(identifier)
        return check.model_copy(deep=True) if check else None

    def listing(self, tenant):
        return [{"check_id": name, "revision": c.revision,
                 "status": "disabled" if not c.enabled else "api_required" if c.api_required else "configured",
                 "assurance": "lower_than_authoritative_api"}
                for name, c in sorted(self._checks.get(tenant, {}).items())]

    def available(self, tenant):
        return any(c.enabled and not c.api_required for c in self._checks.get(tenant, {}).values())
