"""Shared client policy. Exceptions deliberately retain no request or response body."""
import hashlib
import json
import math
import re
import threading
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from .provider_sdk import ProviderManifest


class DoneProofError(Exception):
    def __init__(self, code: str, *, status: int | None = None, request_id: str | None = None):
        self.code, self.status = code, status
        self.request_id = request_id if request_id and re.fullmatch(r'req_[a-f0-9]{16,32}', request_id) else None
        super().__init__(code)


class AuthenticationError(DoneProofError):
    pass


class ConflictError(DoneProofError):
    pass


class RateLimitError(DoneProofError):
    pass


class DoneProofTimeout(DoneProofError):
    pass


class VerificationCancelled(DoneProofError):
    pass


class CompatibilityError(DoneProofError):
    pass


class CallbackError(DoneProofError):
    pass


class DuplicateEvent(CallbackError):
    pass


class Cancellation:
    """Thread-safe cooperative cancellation of local waiting; cancel_job stops server work."""
    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        self._event.set()

    def check(self):
        if self._event.is_set():
            raise VerificationCancelled('local_wait_cancelled')


@dataclass(frozen=True)
class RequestLog:
    method: Literal['GET', 'POST']
    status: int | None
    attempt: int
    request_id: str
    duration_ms: float


class ProviderDocument(ProviderManifest):
    fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')


class ProviderCatalog(BaseModel):
    sdk_version: Literal[1]
    providers: list[ProviderDocument]


class ConnectionOnboarding(BaseModel):
    authorization_url: str
    mode: Literal['trusted_browser_settings'] = 'trusted_browser_settings'
    administrator_login_required: Literal[True] = True


def validate_base_url(value):
    parts = urlsplit(value)
    if (parts.username or parts.password or parts.query or parts.fragment or parts.path not in {'', '/'}
            or not parts.hostname or parts.scheme not in {'http', 'https'}
            or parts.scheme == 'http' and parts.hostname not in {'localhost', '127.0.0.1', '::1'}):
        raise ValueError('Use a credential-free HTTPS origin (HTTP only for loopback development)')
    return value.rstrip('/')


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value):
        raise ValueError('Invalid resource identifier')
    return value


def mutation_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', value):
        raise ValueError('An explicit stable idempotency key is required for this business operation')
    return value


def verification_key(session_id, body):
    return 'verify:' + hashlib.sha256(json.dumps([session_id, body], sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def duration(value):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError('Timeout must be finite and positive')
    return float(value)
