"""Supported asynchronous client. sdk_sync.py is generated from this module."""
from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Callable, TypeVar
from uuid import uuid4

import httpx
from pydantic import BaseModel, JsonValue

from . import __version__
from .assurance_models import AssuranceSession, DurableJob, PrepareSession, ReverifySession, VerifySession
from .compilation_models import CompilationResult
from .connection_api import ConnectionList, ConnectionView, DisconnectRequest
from .domain import CapabilityResponse, VerificationReceipt
from .job_models import TERMINAL
from .retries import retry_after_seconds
from .sdk_common import (
    AuthenticationError,
    Cancellation,
    CompatibilityError,
    ConflictError,
    ConnectionOnboarding,
    DoneProofError,
    DoneProofTimeout,
    ProviderCatalog,
    RateLimitError,
    RequestLog,
    duration,
    identifier,
    mutation_key,
    validate_base_url,
    verification_key,
)
from .signing import ReceiptSigner

T = TypeVar('T', bound=BaseModel)


def payload(model, **values):
    try:
        return model(**values).model_dump(mode='json')
    except ValueError:
        raise DoneProofError('invalid_client_input') from None


class AsyncDoneProof:
    def __init__(self, *, api_key: str, base_url: str = 'https://www.getdoneproof.com', timeout: float = 30,
                 transport: httpx.AsyncBaseTransport | None = None, log_hook: Callable[[RequestLog], None] | None = None):
        self.base_url, self.timeout = validate_base_url(base_url), duration(timeout)
        self._http = httpx.AsyncClient(base_url=self.base_url, headers={'X-DoneProof-Key': api_key,
            'Accept': 'application/json', 'User-Agent': 'doneproof-python/' + __version__},
            timeout=self.timeout, follow_redirects=False, trust_env=False, transport=transport)
        self._log_hook = log_hook
        self.assurance = Assurance(self)
        self.providers = Providers(self)

    async def close(self):
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def _sleep(self, seconds: float, until: float, cancellation: Cancellation | None):
        end = min(until, time.monotonic() + seconds)
        while time.monotonic() < end:
            if cancellation:
                cancellation.check()
            await asyncio.sleep(min(0.1, end - time.monotonic()))

    async def _request(self, method: str, path: str, model: type[T], *, body=None, key=None,
                       until=None, cancellation=None) -> T:
        until = until if until is not None else time.monotonic() + self.timeout
        try:
            async with asyncio.timeout(max(0, until - time.monotonic())):
                if cancellation is not None:
                    async def watch():
                        while True:
                            cancellation.check()
                            await asyncio.sleep(0.05)
                    request = asyncio.create_task(self._request_inner(method, path, model, body=body, key=key,
                                                                      until=until, cancellation=cancellation))
                    watcher = asyncio.create_task(watch())
                    try:
                        done, _ = await asyncio.wait({request, watcher}, return_when=asyncio.FIRST_COMPLETED)
                        if watcher in done:
                            watcher.result()
                        return await request
                    finally:
                        request.cancel()
                        watcher.cancel()
                        await asyncio.gather(request, watcher, return_exceptions=True)
                return await self._request_inner(method, path, model, body=body, key=key,
                                                 until=until, cancellation=cancellation)
        except TimeoutError:
            raise DoneProofTimeout('request_deadline_exceeded') from None

    async def _request_inner(self, method: str, path: str, model: type[T], *, body=None, key=None,
                             until=None, cancellation=None) -> T:
        until = until if until is not None else time.monotonic() + self.timeout
        request_id = 'req_' + uuid4().hex
        headers = {'X-Request-ID': request_id}
        if key:
            headers['Idempotency-Key'] = mutation_key(key)
        retry_safe = method == 'GET' or key is not None
        for attempt in range(4):
            if cancellation:
                cancellation.check()
            remaining = until - time.monotonic()
            if remaining <= 0:
                raise DoneProofTimeout('request_deadline_exceeded', request_id=request_id)
            started, status, retry_after = time.monotonic(), None, 0.0
            try:
                # Bound response memory and total wall time as well as httpx socket timeouts.
                async with self._http.stream(method, path, json=body, headers=headers,
                                             timeout=min(self.timeout, remaining)) as response:
                    status = response.status_code
                    retry_after = retry_after_seconds(response.headers)
                    raw = bytearray()
                    if 200 <= status < 300:
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 16 * 1024 * 1024:
                                raise CompatibilityError('response_too_large', request_id=request_id)
                            if time.monotonic() >= until:
                                raise DoneProofTimeout('request_deadline_exceeded', request_id=request_id)
                            if cancellation:
                                cancellation.check()
                        try:
                            if model is AssuranceSession:
                                value = json.loads(raw)
                                if not isinstance(value, dict) or value.get('protocol_version') != '1.0':
                                    raise ValueError('Unsupported protocol')
                            return model.model_validate_json(raw)
                        except ValueError:
                            raise CompatibilityError('unsupported_or_invalid_response', request_id=request_id) from None
            except httpx.TransportError:
                status = None
            finally:
                if self._log_hook:
                    # No URL, body, provider content, key, headers or error text reaches hooks.
                    self._log_hook(RequestLog(method, status, attempt + 1, request_id, (time.monotonic() - started) * 1000))
            transient = status is None or status == 429 or status is not None and 500 <= status <= 599
            if not transient or not retry_safe or attempt == 3:
                error = AuthenticationError if status in {401, 403} else ConflictError if status == 409 else RateLimitError if status == 429 else DoneProofError
                raise error('request_rejected' if status is not None else 'network_unavailable', status=status, request_id=request_id)
            delay = max(retry_after, min(4.0, 0.25 * 2 ** attempt) * (0.5 + random.random() / 2))
            if delay >= until - time.monotonic():
                raise DoneProofTimeout('retry_exceeds_deadline', status=status, request_id=request_id)
            await self._sleep(delay, until, cancellation)
        raise DoneProofError('request_unavailable')  # defensive exhaustiveness

    async def capabilities(self) -> CapabilityResponse:
        return await self._request('GET', '/v1/capabilities', CapabilityResponse)

    async def get_job(self, job_id: str) -> DurableJob:
        return await self._request('GET', '/v1/jobs/' + identifier(job_id), DurableJob)

    async def cancel_job(self, job_id: str) -> DurableJob:
        # Cancellation is already a convergent server-side mutation.
        return await self._request('POST', '/v1/jobs/' + identifier(job_id) + '/cancel', DurableJob,
                                   key='cancel:' + identifier(job_id))

    async def wait_for_verification(self, job_id: str, *, timeout: float = 120,
                                    cancellation: Cancellation | None = None) -> DurableJob:
        return await self._wait(job_id, time.monotonic() + duration(timeout), cancellation)

    async def _wait(self, job_id, until, cancellation):
        path = '/v1/jobs/' + identifier(job_id)
        delay = 0.25
        while True:
            job = await self._request('GET', path, DurableJob, until=until, cancellation=cancellation)
            if job.state in TERMINAL:
                return job
            await self._sleep(delay * (0.5 + random.random() / 2), until, cancellation)
            delay = min(5.0, delay * 2)

    async def list_connections(self) -> ConnectionList:
        return await self._request('GET', '/v1/connections', ConnectionList)

    async def connection_status(self, connection_id: str) -> ConnectionView:
        return await self._request('GET', '/v1/connections/' + identifier(connection_id), ConnectionView)

    async def begin_connection(self, provider: str) -> ConnectionOnboarding:
        connections = await self.list_connections()
        if not any(p.provider == provider and p.onboarding_available for p in connections.providers):
            raise DoneProofError('connection_onboarding_unavailable')
        # Authorization must begin in the same browser that receives the HttpOnly
        # binding cookie. Never return an OAuth URL generated in this HTTP client.
        return ConnectionOnboarding(authorization_url=self.base_url + '/connections#' + identifier(provider))

    async def disconnect(self, connection_id: str, *, expected_revision: int, idempotency_key: str) -> ConnectionView:
        return await self._request('POST', '/v2/connections/' + identifier(connection_id) + '/disconnect', ConnectionView,
            body=payload(DisconnectRequest, expected_revision=expected_revision), key=mutation_key(idempotency_key))

    @staticmethod
    def verify_receipt(receipt: VerificationReceipt, pinned_public_key: str) -> bool:
        return isinstance(receipt, VerificationReceipt) and ReceiptSigner.verify_trusted(receipt, pinned_public_key)


class Assurance:
    def __init__(self, client: AsyncDoneProof):
        self._client = client

    async def prepare(self, *, task: str, idempotency_key: str, context: dict[str, JsonValue] | None = None,
                      require_transition: bool = False, timeout: float = 150, cancellation: Cancellation | None = None) -> AssuranceSession:
        until = time.monotonic() + duration(timeout)
        result = await self._client._request('POST', '/v1/assurance/sessions', AssuranceSession,
            body=payload(PrepareSession, task=task, context=context or {}, require_transition=require_transition), key=mutation_key(idempotency_key),
            until=until, cancellation=cancellation)
        delay = 0.25
        while result.state == 'PREPARING':
            await self._client._sleep(delay * random.uniform(0.5, 1), until, cancellation)
            result = await self._client._request('GET', '/v1/assurance/sessions/' + identifier(result.id),
                AssuranceSession, until=until, cancellation=cancellation)
            delay = min(5, delay * 2)
        return result

    async def get(self, session_id: str) -> AssuranceSession:
        return await self._client._request('GET', '/v1/assurance/sessions/' + identifier(session_id), AssuranceSession)

    async def verify(self, session_id: str, *, wait: bool = False, timeout: float = 120,
                     deadline_seconds: int = 300, callback_id: str | None = None,
                     cancellation: Cancellation | None = None) -> AssuranceSession:
        until = time.monotonic() + duration(timeout)
        body = payload(VerifySession, deadline_seconds=deadline_seconds, callback_id=callback_id)
        result = await self._client._request('POST', '/v1/assurance/sessions/' + identifier(session_id) + '/verify',
            AssuranceSession, body=body, key=verification_key(session_id, body), until=until, cancellation=cancellation)
        return await self._finish(result, wait, until, cancellation)

    async def reverify(self, session_id: str, *, previous_receipt_id: str, idempotency_key: str,
                       wait: bool = False, timeout: float = 120, deadline_seconds: int = 300,
                       callback_id: str | None = None, cancellation: Cancellation | None = None) -> AssuranceSession:
        until = time.monotonic() + duration(timeout)
        body = payload(ReverifySession, previous_receipt_id=previous_receipt_id,
                       deadline_seconds=deadline_seconds, callback_id=callback_id)
        result = await self._client._request('POST', '/v1/assurance/sessions/' + identifier(session_id) + '/reverify',
            AssuranceSession, body=body, key=mutation_key(idempotency_key), until=until, cancellation=cancellation)
        return await self._finish(result, wait, until, cancellation)

    async def _finish(self, result, wait, until, cancellation):
        if wait and result.current_job_id:
            await self._client._wait(result.current_job_id, until, cancellation)
            return await self._client._request('GET', '/v1/assurance/sessions/' + identifier(result.id),
                                               AssuranceSession, until=until, cancellation=cancellation)
        return result

    async def wait_for_verification(self, session_id: str, *, timeout: float = 120,
                                    cancellation: Cancellation | None = None) -> AssuranceSession:
        until = time.monotonic() + duration(timeout)
        result = await self._client._request('GET', '/v1/assurance/sessions/' + identifier(session_id),
                                           AssuranceSession, until=until, cancellation=cancellation)
        if not result.current_job_id:
            raise ConflictError('session_has_no_verification_job')
        return await self._finish(result, True, until, cancellation)


class Providers:
    def __init__(self, client: AsyncDoneProof):
        self._client, self._catalog, self._until = client, None, 0.0

    async def available(self) -> CapabilityResponse:
        # Connection state is live, so it is never cached here.
        return await self._client.capabilities()

    async def declarations(self, *, refresh: bool = False) -> ProviderCatalog:
        if refresh or self._catalog is None or time.monotonic() >= self._until:
            self._catalog = await self._client._request('GET', '/v1/providers', ProviderCatalog)
            self._until = time.monotonic() + 60
        return self._catalog.model_copy(deep=True)

    async def for_task(self, *, task: str, context: dict[str, JsonValue] | None = None) -> CompilationResult:
        # Planning is authoritative on the server; no local provider heuristics.
        return await self._client._request('POST', '/v2/contracts/compile', CompilationResult,
            body=payload(PrepareSession, task=task, context=context or {}))
