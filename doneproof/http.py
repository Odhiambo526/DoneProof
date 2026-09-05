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


async def resilient_get(client: httpx.AsyncClient, url: str, *, attempts: int = 3, **kwargs: Any) -> httpx.Response:
    durable = durable_observation.get()
    policy = RetryPolicy(attempts, 0.15, 2.0)
    for attempt in range(1, (1 if durable else attempts) + 1):
        try:
            response = await client.get(url, **kwargs)
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

