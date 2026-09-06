"""Registry-dispatched OAuth; no provider selection comes from credentials."""
from .adapters.builtin_oauth import GMAIL_SCOPE as GMAIL_SCOPE
from .provider_errors import ProviderFailure
from .provider_registry import default_registry


class OAuthProviders:
    def __init__(self, settings, transport=None, registry=None):
        self.settings, self.transport = settings, transport
        self.registry = registry or default_registry()

    def backend(self, provider):
        definition = self.registry.get(provider)
        if not definition or not definition.connection_factory:
            raise ProviderFailure("provider_unavailable")
        return definition.connection_factory(self.settings, self.transport)

    def configured(self, provider):
        definition = self.registry.get(provider)
        return bool(definition and definition.connection_factory and self.backend(provider).configured(provider))

    def authorize_url(self, provider, state, verifier, redirect_uri):
        from urllib.parse import urlsplit
        url = self.backend(provider).authorize_url(provider, state, verifier, redirect_uri)
        parts = urlsplit(url)
        origin = self.registry.require(provider).manifest.authentication.authorization_origin
        if parts.scheme + "://" + parts.netloc != origin or parts.username or parts.password or parts.fragment:
            raise ProviderFailure("invalid_provider_response")
        return url

    async def exchange(self, provider, code, verifier, redirect_uri):
        return await self.backend(provider).exchange(provider, code, verifier, redirect_uri)

    async def refresh(self, provider, credentials):
        return await self.backend(provider).refresh(provider, credentials)

    async def identity(self, provider, credentials):
        return await self.backend(provider).identity(provider, credentials)

    async def revoke(self, provider, credentials):
        return await self.backend(provider).revoke(provider, credentials)
