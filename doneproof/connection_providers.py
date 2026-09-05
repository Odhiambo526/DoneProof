"""Provider-owned OAuth endpoints only; untrusted response bodies never become errors."""
from __future__ import annotations

import base64
import hashlib
import time
from urllib.parse import urlencode

import httpx

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TOKEN_URLS = {"gmail": "https://oauth2.googleapis.com/token", "github": "https://github.com/login/oauth/access_token"}
AUTH_URLS = {"gmail": "https://accounts.google.com/o/oauth2/v2/auth", "github": "https://github.com/login/oauth/authorize"}


class ProviderFailure(Exception):
    def __init__(self, code="provider_unavailable", reconnect=False):
        super().__init__(code)
        self.code = code
        self.reconnect = reconnect


class OAuthProviders:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport

    def client_credentials(self, provider):
        if provider == "gmail":
            return self.settings.google_client_id, self.settings.google_client_secret
        return self.settings.github_client_id, self.settings.github_client_secret

    def configured(self, provider):
        return all(self.client_credentials(provider)) and (
            provider != "github" or bool(self.settings.github_app_slug))

    def authorize_url(self, provider, state, verifier, redirect_uri):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        args = {"client_id": self.client_credentials(provider)[0], "redirect_uri": redirect_uri,
                "response_type": "code", "state": state, "code_challenge": challenge,
                "code_challenge_method": "S256"}
        if provider == "gmail":
            args.update(scope=GMAIL_SCOPE, access_type="offline", prompt="consent")
        else:
            args["prompt"] = "select_account"
        return AUTH_URLS[provider] + "?" + urlencode(args)

    async def request(self, method, url, **kwargs):
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, transport=self.transport,
                    headers={"Accept": "application/json", "User-Agent": "DoneProof-connections"}) as client:
                response = await client.request(method, url, **kwargs)
            if response.status_code in {401, 403}:
                raise ProviderFailure("authorization_required", True)
            if response.status_code >= 300:
                # Inspect only the standard OAuth error identifier, never propagate its description.
                invalid = False
                try:
                    invalid = response.json().get("error") in {"invalid_grant", "bad_refresh_token", "invalid_token"}
                except (ValueError, AttributeError, TypeError):
                    pass
                raise ProviderFailure("authorization_required" if invalid else "provider_unavailable", invalid)
            if len(response.content) > 1024 * 1024:
                raise ProviderFailure("invalid_provider_response")
            return response
        except httpx.HTTPError:
            raise ProviderFailure() from None

    @staticmethod
    def token_payload(data, previous=None):
        if not isinstance(data, dict) or data.get("error"):
            raise ProviderFailure("authorization_required", True)
        if not data.get("access_token"):
            raise ProviderFailure("invalid_provider_response")
        result = dict(previous or {})
        for name in ("access_token", "refresh_token"):
            value = data.get(name)
            if value is not None:
                if not isinstance(value, str) or not value or len(value) > 16384 or any(ord(c) < 33 for c in value):
                    raise ProviderFailure("invalid_provider_response")
                result[name] = value
        if not result.get("access_token"):
            raise ProviderFailure("invalid_provider_response")
        if str(data.get("token_type", "bearer")).lower() != "bearer":
            raise ProviderFailure("invalid_provider_response")
        now = int(time.time())
        for name, target in (("expires_in", "expires_at"), ("refresh_token_expires_in", "refresh_expires_at")):
            if name in data:
                value = data[name]
                if isinstance(value, bool):
                    raise ProviderFailure("invalid_provider_response")
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    raise ProviderFailure("invalid_provider_response") from None
                if not 0 < value <= 315360000:
                    raise ProviderFailure("invalid_provider_response")
                result[target] = now + value
            elif not previous:
                result[target] = None
        if "scope" in data:
            if not isinstance(data["scope"], str) or len(data["scope"]) > 8192:
                raise ProviderFailure("invalid_provider_response")
            result["scopes"] = sorted(set(data["scope"].replace(",", " ").split()))
        else:
            result.setdefault("scopes", [])
        return result

    async def exchange(self, provider, code, verifier, redirect_uri):
        client_id, secret = self.client_credentials(provider)
        response = await self.request("POST", TOKEN_URLS[provider], data={
            "client_id": client_id, "client_secret": secret, "code": code,
            "code_verifier": verifier, "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
        try:
            result = self.token_payload(response.json())
        except ValueError:
            raise ProviderFailure("invalid_provider_response") from None
        result["kind"] = "oauth"
        return result

    async def refresh(self, provider, credentials):
        client_id, secret = self.client_credentials(provider)
        response = await self.request("POST", TOKEN_URLS[provider], data={
            "client_id": client_id, "client_secret": secret, "grant_type": "refresh_token",
            "refresh_token": credentials["refresh_token"]})
        try:
            return self.token_payload(response.json(), credentials)
        except ValueError:
            raise ProviderFailure("invalid_provider_response") from None

    async def identity(self, provider, credentials):
        headers = {"Authorization": "Bearer " + credentials["access_token"]}
        try:
            if provider == "gmail":
                if credentials.get("kind") == "oauth":
                    if GMAIL_SCOPE not in credentials["scopes"]:
                        raise ProviderFailure("insufficient_scope", True)
                    allowed = {GMAIL_SCOPE, "openid", "email", "profile",
                               "https://www.googleapis.com/auth/userinfo.email",
                               "https://www.googleapis.com/auth/userinfo.profile"}
                    if not set(credentials["scopes"]) <= allowed:
                        raise ProviderFailure("read_only_scope_required", True)
                response = await self.request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile", headers=headers)
                account = response.json()["emailAddress"]
                if not isinstance(account, str) or "@" not in account or len(account) > 320:
                    raise ValueError
                return account.casefold(), account
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            user = (await self.request("GET", "https://api.github.com/user", headers=headers)).json()
            account = str(user["id"])
            label = user["login"]
            if not account.isdigit() or not isinstance(label, str) or not label or len(label) > 100:
                raise ValueError
            if credentials.get("kind") == "oauth":
                data = (await self.request("GET", "https://api.github.com/user/installations?per_page=100",
                                          headers=headers)).json()
                installs = data["installations"]
                if not installs or data["total_count"] > 100:
                    raise ProviderFailure("installation_required", True)
                for install in installs:
                    permissions = install["permissions"]
                    if (permissions.get("issues") != "read" or permissions.get("pull_requests") != "read"
                            or any(value != "read" for value in permissions.values())):
                        raise ProviderFailure("read_only_installation_required", True)
            return account, label
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ProviderFailure("invalid_provider_response") from None

    async def revoke(self, provider, credentials):
        if provider == "gmail":
            # Form body keeps the token out of URL/access logs.
            await self.request("POST", "https://oauth2.googleapis.com/revoke",
                data={"token": credentials.get("refresh_token") or credentials["access_token"]})
            return
        if credentials.get("kind") != "oauth":
            raise ProviderFailure("revoke_in_github_settings")
        client_id, secret = self.client_credentials(provider)
        if not client_id or not secret:
            raise ProviderFailure("oauth_configuration_required")
        await self.request("DELETE", f"https://api.github.com/applications/{client_id}/grant",
            auth=httpx.BasicAuth(client_id, secret), json={"access_token": credentials["access_token"]})
