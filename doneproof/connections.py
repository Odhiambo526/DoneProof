from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from dataclasses import replace
from urllib.parse import urlsplit

import httpx

from .adapters.base import ProviderAdapter, ProviderObservation
from .adapters.github import GitHubAdapter
from .adapters.gmail import GmailAdapter
from .connection_crypto import CredentialVault
from .connection_providers import OAuthProviders, ProviderFailure
from .connection_store import ConnectionStore


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ConnectionConflict(Exception):
    pass


class ConnectionService:
    def __init__(self, store, settings, transport=None):
        self.db = ConnectionStore(store)
        self.settings = settings
        self.vault = CredentialVault(settings.connection_encryption_keys, settings.connection_active_key)
        self.providers = OAuthProviders(settings, transport)
        self.validate_configuration()
        self.import_legacy()

    def validate_configuration(self):
        admins = self.settings.connection_admin_keys
        if (not isinstance(admins, dict)
                or any(not isinstance(k, str) or not k or not isinstance(t, str) or not t
                       for k, t in admins.items())
                or set(admins) & set(self.settings.api_keys)):
            raise RuntimeError("Connection administrator keys must be distinct from verification keys")
        base = self.settings.connection_public_url
        if base:
            parts = urlsplit(base)
            local = not self.settings.is_production and parts.hostname in {"localhost", "127.0.0.1"}
            if (parts.scheme != "https" and not (local and parts.scheme == "http")
                    or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment
                    or parts.path not in {"", "/"}):
                raise RuntimeError("DONEPROOF_PUBLIC_URL must be a fixed HTTPS origin")
        for client_id in (self.settings.google_client_id, self.settings.github_client_id):
            if client_id and not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", client_id):
                raise RuntimeError("Invalid OAuth client identifier")
        if self.settings.github_app_slug and not re.fullmatch(r"[a-zA-Z0-9-]{1,100}", self.settings.github_app_slug):
            raise RuntimeError("Invalid GitHub App slug")

    def configured(self, provider):
        return bool(self.vault.available and self.settings.connection_public_url
                    and self.providers.configured(provider))

    def redirect_uri(self, provider):
        return self.settings.connection_public_url.rstrip("/") + f"/v1/connections/oauth/{provider}/callback"

    def import_legacy(self):
        # One-time, explicitly scoped import. No runtime environment fallback.
        imports = [(tenant, "gmail", token) for tenant, token in self.settings.gmail_tokens.items()]
        if self.settings.gmail_access_token or self.settings.github_token:
            tenants = set(self.settings.api_keys.values()) | set(self.settings.connection_admin_keys.values())
            tenant = self.settings.legacy_connection_tenant
            if not tenant:
                tenant = next(iter(tenants)) if len(tenants) == 1 else ("default" if not tenants else None)
            if not tenant:
                raise RuntimeError("Global legacy tokens require DONEPROOF_LEGACY_CONNECTION_TENANT")
            if self.settings.gmail_access_token and tenant not in self.settings.gmail_tokens:
                imports.append((tenant, "gmail", self.settings.gmail_access_token))
            if self.settings.github_token:
                imports.append((tenant, "github", self.settings.github_token))
        if imports and not self.vault.available:
            raise RuntimeError("Legacy connector import requires connection encryption keys")
        for tenant, provider, token in imports:
            row = self.db.ensure(tenant, provider)
            # Never resurrect a previously disconnected connection on cold starts.
            if row["revision"] != 0:
                continue
            payload = {"access_token": token, "kind": "legacy", "scopes": []}
            updated = self.db.update(row, credential_ciphertext=self.vault.encrypt(row, payload),
                                     state="error", error_code="health_check_required")
            if updated:
                self.db.audit(updated, "imported")

    def start(self, tenant, provider):
        row = self.db.ensure(tenant, provider)
        if row["revocation_pending"]:
            raise ConnectionConflict
        state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        encrypted = self.vault.encrypt(row, {"verifier": verifier}, "oauth")
        if not self.db.start_oauth(row, digest(state), digest(browser), encrypted, self.redirect_uri(provider)):
            raise ConnectionConflict
        return self.providers.authorize_url(provider, state, verifier, self.redirect_uri(provider)), browser

    async def callback(self, provider, state, browser, code, denied=False):
        entry = self.db.consume_oauth(provider, digest(state), digest(browser))
        if not entry:
            return False
        row = self.db.get(entry["tenant_id"], connection_id=entry["connection_id"])
        if row["authorization_version"] != entry["authorization_version"]:
            return False
        if denied or not code:
            self.db.audit(row, "authorization_declined")
            return False
        credentials = None
        try:
            verifier = self.vault.decrypt(row, entry["verifier_ciphertext"], "oauth")["verifier"]
            credentials = await self.providers.exchange(provider, code, verifier, entry["redirect_uri"])
            # Require offline access for managed Gmail onboarding; an access-only grant cannot refresh.
            if provider == "gmail" and not credentials.get("refresh_token"):
                raise ProviderFailure("offline_access_required", True)
            account_id, label = await self.providers.identity(provider, credentials)
            if row["account_id"] and row["account_id"] != account_id and row["credential_ciphertext"]:
                raise ProviderFailure("disconnect_before_account_change", True)
            updated = self.save_credentials(row, credentials, account_id, label)
            if not updated:
                raise ConnectionConflict
            self.db.audit(updated, "connected")
            return True
        except (ProviderFailure, RuntimeError, ConnectionConflict) as exc:
            if credentials:
                try:
                    await self.providers.revoke(provider, credentials)
                except ProviderFailure:
                    # Failed cleanup remains encrypted and retryable, even if a newer callback won.
                    self.db.queue_revocation(row, self.vault.encrypt(row, credentials))
            if isinstance(exc, ProviderFailure) and not row["credential_ciphertext"]:
                updated = self.db.update(row, state="reconnect_required" if exc.reconnect else "error",
                                         error_code=exc.code)
                if updated:
                    self.db.audit(updated, "authorization_failed")
            return False

    def save_credentials(self, row, credentials, account_id, label):
        return self.db.update(row, state="connected", account_id=account_id, account_label=label,
            credential_ciphertext=self.vault.encrypt(row, credentials), scopes_json=json.dumps(credentials["scopes"]),
            expires_at=credentials.get("expires_at"), refresh_expires_at=credentials.get("refresh_expires_at"),
            last_checked_at=int(time.time()), error_code=None, revocation_pending=0)

    def fail(self, row, failure, credentials=None):
        retained = {}
        if credentials:
            retained = {"credential_ciphertext": self.vault.encrypt(row, credentials),
                        "expires_at": credentials.get("expires_at"),
                        "refresh_expires_at": credentials.get("refresh_expires_at")}
        updated = self.db.update(row, state="reconnect_required" if failure.reconnect else "error",
                                 error_code=failure.code, **retained)
        if updated:
            self.db.audit(updated, "unavailable")
        elif credentials:
            self.db.queue_revocation(row, self.vault.encrypt(row, credentials))

    async def usable(self, tenant, provider, *, check_health=False):
        # Leases coordinate refresh across processes. A disconnected or superseded generation always wins.
        for _ in range(20):
            row = self.db.get(tenant, provider=provider)
            if not row or row["state"] in {"disabled", "reconnect_required"} or not row["credential_ciphertext"]:
                return None
            try:
                credentials = self.vault.decrypt(row)
            except RuntimeError:
                self.fail(row, ProviderFailure("credential_unavailable"))
                return None
            now = int(time.time())
            refresh = row["expires_at"] is not None and row["expires_at"] <= now + 60
            check = check_health or row["state"] != "connected" or not row["last_checked_at"] or row["last_checked_at"] <= now - 300
            if not refresh and not check:
                return row, credentials
            if row["lease_id"]:
                if row["lease_until"] <= now:
                    # A crashed refresh may have rotated the upstream token: never replay it.
                    self.fail(row, ProviderFailure("refresh_interrupted", True))
                    return None
                await asyncio.sleep(0.1)
                continue
            if refresh and (not credentials.get("refresh_token") or
                    row["refresh_expires_at"] is not None and row["refresh_expires_at"] <= now):
                updated = self.db.update(row, state="expired", error_code="token_expired")
                if updated:
                    self.db.audit(updated, "expired")
                return None
            leased = self.db.acquire_lease(row)
            if not leased:
                continue
            refreshed = False
            try:
                if refresh:
                    if not self.providers.configured(provider):
                        raise ProviderFailure("oauth_configuration_required")
                    credentials = await self.providers.refresh(provider, credentials)
                    refreshed = True
                account_id, label = await self.providers.identity(provider, credentials)
                if row["account_id"] and row["account_id"] != account_id:
                    raise ProviderFailure("account_changed", True)
                updated = self.save_credentials(leased, credentials, account_id, label)
                if updated:
                    self.db.audit(updated, "refreshed" if refresh else "health_checked")
                    return updated, credentials
                # A refresh finishing after disconnect must also revoke its freshly rotated token.
                current = self.db.get(tenant, provider=provider)
                if refresh and current["state"] == "disabled":
                    try:
                        await self.providers.revoke(provider, credentials)
                        self.db.update(current, credential_ciphertext=None,
                                       revocation_pending=int(bool(self.db.revocations(current))), error_code=None)
                    except ProviderFailure:
                        # Retain rotated credentials solely for a subsequent revoke retry.
                        self.db.update(current, credential_ciphertext=self.vault.encrypt(current, credentials),
                                       revocation_pending=1, error_code="revocation_pending")
                elif refresh:
                    self.db.queue_revocation(row, self.vault.encrypt(row, credentials))
                return None
            except ProviderFailure as exc:
                # Network ambiguity during a rotating refresh requires new authorization.
                if refresh and exc.code in {"provider_unavailable", "invalid_provider_response"}:
                    exc = ProviderFailure("refresh_interrupted", True)
                self.fail(leased, exc, credentials if refreshed else None)
                return None
        return None

    async def disconnect(self, tenant, connection_id):
        row = self.db.get(tenant, connection_id=connection_id)
        if not row:
            return None
        disabled = self.db.disable(row)
        self.db.audit(disabled, "disabled")
        for pending in self.db.revocations(disabled):
            try:
                credentials = self.vault.decrypt(disabled, pending["credential_ciphertext"])
                await self.providers.revoke(disabled["provider"], credentials)
                self.db.remove_revocation(disabled, pending["id"])
            except (ProviderFailure, RuntimeError):
                pass
        if disabled["credential_ciphertext"]:
            try:
                credentials = self.vault.decrypt(disabled)
                await self.providers.revoke(disabled["provider"], credentials)
                self.db.update(disabled, credential_ciphertext=None,
                               revocation_pending=int(bool(self.db.revocations(disabled))),
                               expires_at=None, refresh_expires_at=None, error_code=None)
            except (ProviderFailure, RuntimeError) as exc:
                code = exc.code if isinstance(exc, ProviderFailure) else "credential_unavailable"
                self.db.update(disabled, error_code=code)
        current = self.db.get(tenant, connection_id=connection_id)
        if self.db.revocations(current):
            self.db.update(current, revocation_pending=1, error_code="revocation_pending")
        elif not current["credential_ciphertext"]:
            self.db.update(current, revocation_pending=0, error_code=None)
        return self.db.get(tenant, connection_id=connection_id)

    def capability(self, tenant, provider):
        row = self.db.get(tenant, provider=provider)
        if not row:
            # Existing unauthenticated public GitHub reads remain compatible.
            return "available" if provider == "github" else "configuration_required"
        if row["state"] == "disabled":
            return "disabled"
        if not self.vault.available or not row["credential_ciphertext"] or not row["account_id"]:
            return "configuration_required"
        if self.db.public(row)["state"] != "connected":
            return "configuration_required"
        try:
            self.vault.decrypt(row)
        except RuntimeError:
            return "configuration_required"
        return "available"


class ManagedAdapter(ProviderAdapter):
    def __init__(self, service, provider):
        self.service = service
        self.provider = provider

    @staticmethod
    def unavailable():
        return ProviderObservation(None, note="The workspace connection is unavailable or changed; reconnect and register a new run if needed.",
                                   indeterminate=True)

    async def observe(self, selector, context):
        service = self.service
        initial = service.db.get(context.tenant_id, provider=self.provider)
        if not initial and self.provider == "github":
            identity = "github:public"
            row, credentials = None, None
        else:
            resolved = await service.usable(context.tenant_id, self.provider)
            if not resolved:
                return self.unavailable()
            row, credentials = resolved
            identity = digest(json.dumps([row["id"], row["account_id"]]))
        if context.require_connection_binding and not service.db.bind(
                context.tenant_id, context.contract_id, context.condition_id, self.provider, identity,
                context.capture_connection_binding):
            return self.unavailable()
        auth_failed = False

        async def response_hook(response):
            nonlocal auth_failed
            if response.status_code in {401, 403}:
                auth_failed = True

        # Per-request adapter and client credentials: never mutate shared adapter state.
        if self.provider == "github":
            adapter = GitHubAdapter(token=credentials["access_token"] if credentials else None,
                                    transport=service.providers.transport, allow_env=False,
                                    response_hooks=[response_hook])
        else:
            scoped = replace(service.settings, gmail_access_token=None,
                             gmail_tokens={context.tenant_id: credentials["access_token"]})
            adapter = GmailAdapter(scoped, transport=service.providers.transport, response_hooks=[response_hook])
        try:
            observation = await adapter.observe(selector, context)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            if row and not auth_failed:
                service.fail(row, ProviderFailure())
            observation = self.unavailable()
        if auth_failed and row:
            service.fail(row, ProviderFailure("authorization_required", True))
            return self.unavailable()
        current = service.db.get(context.tenant_id, provider=self.provider)
        if row and (not current or current["revision"] != row["revision"] or current["state"] != "connected"):
            return self.unavailable()
        if not row and current:
            return self.unavailable()
        return observation
