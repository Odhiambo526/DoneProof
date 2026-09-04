from __future__ import annotations

import asyncio
from typing import Any

import httpx


_RETRYABLE = {429, 500, 502, 503, 504}


async def resilient_get(client: httpx.AsyncClient, url: str, *, attempts: int = 3, **kwargs: Any) -> httpx.Response:
    last: httpx.Response | None = None
    for attempt in range(attempts):
        try:
            response = await client.get(url, **kwargs)
        except (httpx.ConnectError, httpx.ReadTimeout):
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(0.15 * (2**attempt))
            continue
        last = response
        if response.status_code not in _RETRYABLE or attempt == attempts - 1:
            return response
        retry_after = response.headers.get("Retry-After")
        try:
            delay = min(float(retry_after), 2.0) if retry_after else 0.15 * (2**attempt)
        except ValueError:
            delay = 0.15 * (2**attempt)
        await asyncio.sleep(delay)
    assert last is not None
    return last
