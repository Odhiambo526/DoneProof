from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .retries import (
    RetryPolicy,
    TransientObservationError,
    durable_observation,
    transient_exception,
    transient_response,
)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


async def bounded_request(client, method, url, *, max_bytes=MAX_RESPONSE_BYTES, **kwargs):
    """Bound wire bodies before parsing; never include provider content in errors."""
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Accept-Encoding"] = "identity"
    async with client.stream(method, url, headers=headers, **kwargs) as response:
        if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
            raise httpx.DecodingError("Unsupported provider response encoding")
        length = response.headers.get("content-length")
        if length and (not length.isdecimal() or int(length) > max_bytes):
            raise httpx.DecodingError("Provider response exceeds limit")
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            if len(body) + len(chunk) > max_bytes:
                raise httpx.DecodingError("Provider response exceeds limit")
            body.extend(chunk)
        return httpx.Response(response.status_code, headers=response.headers, content=bytes(body),
                              request=response.request, extensions=response.extensions)


async def resilient_get(client: httpx.AsyncClient, url: str, *, attempts: int = 3, **kwargs: Any) -> httpx.Response:
    hooks = kwargs.pop("response_hooks", ())
    durable = durable_observation.get()
    policy = RetryPolicy(attempts, 0.15, 2.0)
    for attempt in range(1, (1 if durable else attempts) + 1):
        try:
            response = await bounded_request(client, "GET", url, **kwargs)
            for hook in hooks:
                await hook(response)
        except httpx.HTTPError as exc:
            if not transient_exception(exc):
                raise
            if durable:
                raise TransientObservationError("provider_network_error") from None
            if attempt == attempts:
                raise
            await asyncio.sleep(policy.delay(attempt))
            continue
        failure = transient_response(response)
        if failure is None:
            return response
        if durable:
            raise failure
        if attempt == attempts:
            return response
        await asyncio.sleep(policy.delay(attempt, failure.retry_after))
    raise RuntimeError("Invalid retry attempt limit")
