"""Retry classification shared by HTTP adapters and the durable worker."""
from __future__ import annotations

import math
import random
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx

durable_observation: ContextVar[bool] = ContextVar("durable_observation", default=False)


class TransientObservationError(Exception):
    def __init__(self, code="provider_unavailable", retry_after=0.0):
        if not isinstance(code, str) or code not in {"provider_unavailable", "provider_rate_limited", "provider_timeout", "provider_network_error"}:
            code = "provider_unavailable"
        super().__init__(code)
        self.code = code
        self.retry_after = max(0.0, retry_after)


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int
    base_seconds: float
    cap_seconds: float

    def delay(self, attempt, retry_after=0.0, rng=random.random):
        # Equal jitter retains a nonzero floor. Provider deadlines are never truncated by our cap.
        ceiling = min(self.cap_seconds, self.base_seconds * 2 ** max(0, attempt - 1))
        return max(retry_after, ceiling * (0.5 + 0.5 * rng()))


class _ProviderPolicies:
    def __getitem__(self, provider):
        from .provider_registry import default_registry
        return default_registry().policy(provider)


POLICIES = _ProviderPolicies()
CALLBACK_POLICY = RetryPolicy(6, 2.0, 300.0)


def retry_after_seconds(headers, now=None):
    now = time.time() if now is None else now
    raw = headers.get("Retry-After")
    value = 0.0
    if raw:
        try:
            value = float(raw)
        except (ValueError, TypeError):
            try:
                parsed = parsedate_to_datetime(raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                value = parsed.timestamp() - now
            except (ValueError, TypeError, OverflowError):
                value = 0.0
    if headers.get("X-RateLimit-Remaining") == "0":
        try:
            value = max(value, float(headers.get("X-RateLimit-Reset", "0")) - now)
        except (ValueError, TypeError):
            pass
    return max(0.0, value) if math.isfinite(value) else 0.0


def transient_response(response, *, provider_errors=True):
    code = response.status_code
    if code == 429 or 500 <= code <= 599 and code not in {501, 505}:
        return TransientObservationError("provider_rate_limited" if code == 429 else "provider_unavailable",
                                         retry_after_seconds(response.headers))
    if not provider_errors:
        return None
    # GitHub secondary limits use 403; ordinary permission denials remain semantic UNKNOWN.
    if code == 403 and response.request.url.host == "api.github.com" and (
            response.headers.get("Retry-After") or response.headers.get("X-RateLimit-Remaining") == "0"):
        return TransientObservationError("provider_rate_limited", max(60.0, retry_after_seconds(response.headers)))
    # Gmail identifies transient quota failures in the structured reason, not every 403.
    if code == 403 and response.request.url.host == "gmail.googleapis.com":
        try:
            reasons = [item.get("reason") for item in response.json().get("error", {}).get("errors", [])]
            if any(reason in {"rateLimitExceeded", "userRateLimitExceeded"} for reason in reasons):
                return TransientObservationError("provider_rate_limited", retry_after_seconds(response.headers))
        except (ValueError, AttributeError, TypeError):
            pass
    return None


def transient_exception(exc):
    return isinstance(exc, (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError))
