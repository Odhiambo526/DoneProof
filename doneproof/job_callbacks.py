"""Completion delivery to operator-configured, tenant-owned HTTPS destinations."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import time
from urllib.parse import urlsplit

import httpx

from .job_store import canonical, digest
from .retries import CALLBACK_POLICY, transient_exception, transient_response


class CallbackRegistry:
    def __init__(self, configuration):
        if not isinstance(configuration, dict):
            raise RuntimeError("Job callbacks must be a tenant mapping")
        self.configuration = configuration
        for tenant, endpoints in configuration.items():
            if not isinstance(tenant, str) or not tenant or not isinstance(endpoints, dict):
                raise RuntimeError("Invalid job callback configuration")
            for identifier, endpoint in endpoints.items():
                if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier)
                        or not isinstance(endpoint, dict)):
                    raise RuntimeError("Invalid job callback configuration")
                url, secret = endpoint.get("url"), endpoint.get("secret")
                if not isinstance(url, str) or not isinstance(secret, str) or len(secret) < 32:
                    raise RuntimeError("Callbacks require an HTTPS URL and a signing secret of at least 32 characters")
                try:
                    parts = urlsplit(url)
                    port = parts.port
                except ValueError:
                    raise RuntimeError("Invalid callback destination") from None
                host = parts.hostname or ""
                if (parts.scheme != "https" or not host or parts.username or parts.password or parts.query
                        or parts.fragment or port not in {None, 443} or len(url) > 2048
                        or any(ord(c) < 33 for c in url)
                        or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
                    raise RuntimeError("Callbacks require a fixed public HTTPS destination without URL credentials or query parameters")
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) or "." not in host:
                        raise RuntimeError("Invalid callback host") from None
                else:
                    if not address.is_global:
                        raise RuntimeError("Callback IP addresses must be public")

    def get(self, tenant, identifier):
        endpoint = self.configuration.get(tenant, {}).get(identifier)
        if not endpoint:
            return None
        return {**endpoint, "fingerprint": digest(canonical([tenant, identifier, endpoint["url"]]))}

    async def deliver(self, db, row, transport=None):
        target = self.get(row["tenant_id"], row["callback_id"])
        if not target or target["fingerprint"] != row["callback_fingerprint"]:
            db.finish_callback(row, "DEAD", "callback_configuration_changed")
            return
        timestamp = str(int(time.time()))
        payload = row["payload_json"].encode()
        signature = hmac.new(target["secret"].encode(), timestamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, transport=transport,
                                         trust_env=False) as client:
                # Stream and ignore response bodies: no callback response is evidence or an error message.
                async with client.stream("POST", target["url"], content=payload, headers={
                    "Content-Type": "application/json", "X-DoneProof-Event": row["event_id"],
                    "X-DoneProof-Timestamp": timestamp, "X-DoneProof-Signature": "sha256=" + signature,
                }) as response:
                    if 200 <= response.status_code < 300:
                        db.finish_callback(row, "DELIVERED")
                        return
                    failure = transient_response(response, provider_errors=False)
                    if not failure:
                        db.finish_callback(row, "DEAD", "callback_rejected")
                        return
                    delay = CALLBACK_POLICY.delay(row["attempts"], failure.retry_after)
        except httpx.HTTPError as exc:
            if not transient_exception(exc):
                db.finish_callback(row, "DEAD", "callback_unavailable")
                return
            delay = CALLBACK_POLICY.delay(row["attempts"])
        db.finish_callback(row, "PENDING" if row["attempts"] < CALLBACK_POLICY.attempts else "DEAD",
                           "callback_unavailable", delay)
