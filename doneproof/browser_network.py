"""Bounded GET-only transport: DNS is checked and the TLS connection pins that IP."""
import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .browser_policy import public_url


class BrowserUnavailable(Exception):
    def __init__(self, code="blocked_request"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BrowserResponse:
    body: bytes
    content_type: str


async def public_addresses(host):
    records = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = sorted({r[4][0] for r in records})
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise BrowserUnavailable()
    return addresses


class BrowserNetwork:
    """One instance per observation; never forwards browser headers, cookies or URLs outside the policy."""
    def __init__(self, check, *, resolve=public_addresses, transport=None):
        self.allowed = frozenset((check.url, *check.resources))
        self.resolve, self.transport = resolve, transport
        self.requests = 0
        self.bytes = 0
        self.limit = asyncio.Semaphore(2)

    async def get(self, url):
        if url not in self.allowed:
            raise BrowserUnavailable()
        public_url(url)
        self.requests += 1
        if self.requests > 32:
            raise BrowserUnavailable()
        async with self.limit:
            async with asyncio.timeout(5):
                host = urlsplit(url).hostname
                addresses = await self.resolve(host)
                # Validate injected/system resolver results here as well as at the DNS boundary.
                if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
                    raise BrowserUnavailable()
                pinned = httpx.URL(url).copy_with(host=addresses[0])
                # A separate pool per request prevents cross-host TLS/SNI reuse for shared IPs.
                async with httpx.AsyncClient(transport=self.transport, timeout=4, follow_redirects=False,
                                             trust_env=False) as client:
                    async with client.stream("GET", pinned, headers={"Host": host, "Accept-Encoding": "identity",
                        "Cache-Control": "no-cache, no-store", "Pragma": "no-cache", "User-Agent": "DoneProof-Browser/1"},
                        extensions={"sni_hostname": host}) as response:
                        if response.status_code in {401, 403}:
                            raise BrowserUnavailable("login_or_challenge")
                        if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                            raise BrowserUnavailable()
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if content_type not in {"text/html", "text/css", "application/javascript", "text/javascript",
                                                "application/json", "image/png", "image/jpeg", "image/svg+xml",
                                                "font/woff", "font/woff2"}:
                            raise BrowserUnavailable()
                        body = bytearray()
                        async for chunk in response.aiter_raw(chunk_size=16384):
                            self.bytes += len(chunk)
                            if len(body) + len(chunk) > 1048576 or self.bytes > 4194304:
                                raise BrowserUnavailable()
                            body.extend(chunk)
                        return BrowserResponse(bytes(body), content_type)
