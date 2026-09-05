"""Stable SDK error codes; upstream text is never a public diagnostic."""

CONNECTION_CODES = frozenset({
    "provider_unavailable", "provider_rate_limited", "authorization_required", "invalid_provider_response",
    "insufficient_scope", "read_only_scope_required", "installation_required", "read_only_installation_required",
    "revoke_in_github_settings", "oauth_configuration_required", "offline_access_required",
    "disconnect_before_account_change", "credential_unavailable", "refresh_interrupted", "account_changed",
})


class ProviderFailure(Exception):
    def __init__(self, code="provider_unavailable", reconnect=False, *, transient=False, retry_after=0):
        code = code if isinstance(code, str) and code in CONNECTION_CODES else "provider_unavailable"
        super().__init__(code)
        self.code = code
        self.reconnect = reconnect
        self.transient = transient
        self.retry_after = retry_after
