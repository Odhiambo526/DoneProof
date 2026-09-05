import asyncio
import base64
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from doneproof.adapters.base import ObservationContext
from doneproof.browser_artifacts import BrowserArtifacts
from doneproof.browser_network import BrowserNetwork, BrowserUnavailable, public_addresses
from doneproof.browser_policy import BrowserCheck, BrowserChecks
from doneproof.store import Store
from tests.browser_helpers import CHECK, PNG, app_for, payload
from tests.test_jobs import A


@pytest.mark.parametrize("url", ["http://example.org/", "https://127.0.0.1/", "https://10.0.0.1/",
    "https://169.254.169.254/", "https://[::1]/", "https://2130706433/", "https://example.org:8443/",
    "https://name:private@example.org/", "https://example.org/?token=private", "https://example.org/#private",
    "https://example.org/../action", "https://example.org/%2Fother", "file:///page.html",
    "https://intranet.local/", "https://example.org./", "https://example.org\\@127.0.0.1/"])
def test_non_public_or_non_exact_configuration_is_rejected(url):
    with pytest.raises(RuntimeError, match="Invalid browser check configuration") as exc:
        BrowserChecks({"tenant-a": {"release-7": {**CHECK, "url": url}}})
    assert url not in str(exc.value) and "private" not in str(exc.value)


@pytest.mark.parametrize("change", [{"no_authoritative_api": "true"}, {"states": {"ready": "Complete"}},
    {"states": {"ready": "Complete", "pending": "Complete"}}, {"selector": "body"},
    {"selector": "#release-id"}, {"script": "operator-script"}, {"storage_state": {}},
    {"resources": ["https://localhost/"]}, {"page_text": " "}, {"success_state": "invented"}])
def test_unsafe_check_shapes_are_rejected(change):
    with pytest.raises(RuntimeError):
        BrowserChecks({"tenant-a": {"release-7": {**CHECK, **change}}})


def test_network_pins_resolved_ip_tls_hostname_and_sends_no_browser_credentials():
    requests = []
    def response(request):
        requests.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "status.example.org"
        assert request.extensions["sni_hostname"] == "status.example.org"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert request.headers["cache-control"] == "no-cache, no-store"
        return httpx.Response(200, headers={"content-type": "text/html", "set-cookie": "private=secret"}, stream=httpx.ByteStream(b"<p>Complete</p>"))
    resolve = AsyncMock(return_value=["93.184.216.34"])
    network = BrowserNetwork(BrowserCheck.model_validate(CHECK), resolve=resolve, transport=httpx.MockTransport(response))
    result = asyncio.run(network.get(CHECK["url"]))
    assert result.body == b"<p>Complete</p>" and result.content_type == "text/html"
    assert len(requests) == 1 and resolve.await_count == 1


@pytest.mark.parametrize("addresses", [[], ["127.0.0.1"], ["93.184.216.34", "10.0.0.1"], ["::1"], ["169.254.169.254"]])
def test_non_global_dns_responses_never_reach_transport(addresses):
    def unexpected(_):
        pytest.fail("A rejected DNS answer must never open a connection")
    network = BrowserNetwork(BrowserCheck.model_validate(CHECK), resolve=AsyncMock(return_value=addresses),
                             transport=httpx.MockTransport(unexpected))
    with pytest.raises(BrowserUnavailable):
        asyncio.run(network.get(CHECK["url"]))


def test_system_resolver_checks_every_answer(monkeypatch):
    async def check():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", AsyncMock(return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]))
        with pytest.raises(BrowserUnavailable):
            await public_addresses("status.example.org")
    asyncio.run(check())


@pytest.mark.parametrize("status,headers,body", [(302, {"location": "https://other.example/"}, b""),
    (401, {}, b"secret error"), (403, {}, b"secret error"), (404, {}, b"Complete"),
    (429, {"retry-after": "120"}, b"Complete"), (500, {}, b"Complete"),
    (200, {"content-type": "application/octet-stream"}, b"Complete"),
    (200, {"content-type": "text/html", "content-encoding": "unsupported"}, b"Complete"),
    (200, {"content-type": "text/html"}, b"x" * 1048577)],
    ids=["redirect", "unauthenticated", "forbidden", "missing", "rate-limited", "server-error", "binary", "encoding", "oversized"])
def test_redirect_errors_unsupported_content_and_oversized_responses_are_unknown(status, headers, body):
    network = BrowserNetwork(BrowserCheck.model_validate(CHECK), resolve=AsyncMock(return_value=["93.184.216.34"]),
        transport=httpx.MockTransport(lambda _: httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))))
    with pytest.raises(BrowserUnavailable):
        asyncio.run(network.get(CHECK["url"]))


def test_network_budget_and_non_allowlisted_destinations():
    network = BrowserNetwork(BrowserCheck.model_validate(CHECK), resolve=AsyncMock(return_value=["93.184.216.34"]),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={"content-type": "text/html"}, stream=httpx.ByteStream(b"ok"))))
    async def check():
        with pytest.raises(BrowserUnavailable):
            await network.get("https://status.example.org/other")
        for _ in range(32):
            await network.get(CHECK["url"])
        with pytest.raises(BrowserUnavailable):
            await network.get(CHECK["url"])
    asyncio.run(check())


def test_artifacts_encryption_tenant_binding_retention_and_migration(connection_settings, monkeypatch):
    store = Store(connection_settings.storage_dsn)
    artifacts = BrowserArtifacts(store, connection_settings)
    context = ObservationContext("tenant-a", "c", "now", condition_id="p1")
    monkeypatch.setattr("doneproof.browser_artifacts.MAX_ARTIFACTS_PER_TENANT", 2)
    first = artifacts.save(context, PNG)
    artifacts.save(context, PNG)
    last = artifacts.save(context, PNG)
    assert artifacts.read_for_operator("tenant-a", first.artifact_id) is None
    assert artifacts.read_for_operator("tenant-a", last.artifact_id) == PNG
    with artifacts.transaction() as con:
        row = artifacts._row(artifacts.execute(con, "SELECT * FROM browser_artifacts WHERE id=?", (last.artifact_id,)))
        assert base64.b64encode(PNG).decode() not in row["ciphertext"]
        artifacts.execute(con, "UPDATE browser_artifacts SET tenant_id=? WHERE id=?", ("tenant-b", last.artifact_id))
    with pytest.raises(RuntimeError):
        artifacts.read_for_operator("tenant-b", last.artifact_id)
    assert artifacts.read_for_operator("tenant-a", last.artifact_id) is None
    with artifacts.transaction() as con:
        artifacts.execute(con, "UPDATE browser_artifacts SET expires_at=0")
    assert artifacts.purge_expired() == 2
    with pytest.raises(ValueError):
        artifacts.save(context, b"not-png")
    with pytest.raises(ValueError):
        artifacts.save(context, PNG + b"x" * 65536)
    # Idempotent additive upgrade retains signed API receipts byte-for-byte.
    app, client, _, _ = app_for(connection_settings)
    old = client.post("/v1/verify", json=payload(), headers=A).json()
    exact = app.state.store.get_receipt("tenant-a", old["receipt_id"]).model_dump_json()
    for _ in range(2):
        upgraded = Store(connection_settings.storage_dsn, app.state.providers)
        assert upgraded.get_receipt("tenant-a", old["receipt_id"]).model_dump_json() == exact
        if artifacts.pg:
            with upgraded._pg_connect() as con:
                assert con.execute("SELECT version FROM schema_migrations WHERE version=6").fetchone()


def test_historical_signatures_survive_new_receipt_models(connection_settings):
    # Fixed legacy payloads omit provenance entirely; serialization must remain exact.
    from doneproof.domain import ConditionResult, Evidence, Predicate, VerificationReceipt, VerificationSummary
    from doneproof.recovery_models import RecoveryInfo
    from doneproof.signing import ReceiptSigner
    signer = ReceiptSigner(connection_settings)
    for version in ("1.0", "1.1"):
        fields = {"schema_version": version, "receipt_id": "vr_legacy", "contract_id": "cc_legacy", "task": "Legacy API verification",
            "verdict": "VERIFIED", "summary": VerificationSummary(total=1, required=1, passed=1, failed=0, unknown=0, providers=["github"]),
            "results": [ConditionResult(id="p1", description="Old condition", required=True, status="PASS",
                predicate=Predicate(op="eq", path="state", expected="closed"), evidence=Evidence(provider="github", selector={}, observed="closed"), reason="Match")]}
        if version == "1.1":
            fields["recovery"] = RecoveryInfo(chain_id="vr_legacy")
        receipt = signer.sign(VerificationReceipt(**fields))
        raw = receipt.model_dump_json()
        assert "provenance" not in raw
        parsed = VerificationReceipt.model_validate_json(raw)
        assert parsed.model_dump_json() == raw and ReceiptSigner.verify(parsed)
        # Simulate a historical signer using an independent raw JSON canonicalization.
        payload_json = json.loads(raw)
        payload_json.pop("signature")
        payload_json.pop("receipt_hash")
        canonical = json.dumps(payload_json, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        assert ReceiptSigner._payload(parsed) == canonical


def test_disabled_checks_and_key_rotation_fail_closed(connection_settings):
    app, _, _, _ = app_for(connection_settings)
    artifacts = app.state.engine.adapters["browser"].artifacts
    ref = artifacts.save(ObservationContext("tenant-a", "c", "now"), PNG)
    rotated = replace(connection_settings, connection_active_key="next",
        connection_encryption_keys={**connection_settings.connection_encryption_keys, "next": base64.b64encode(b"R" * 32).decode()})
    assert BrowserArtifacts(app.state.store, rotated).read_for_operator("tenant-a", ref.artifact_id) == PNG
    retired = replace(rotated, connection_encryption_keys={"next": rotated.connection_encryption_keys["next"]})
    with pytest.raises(RuntimeError):
        BrowserArtifacts(app.state.store, retired).read_for_operator("tenant-a", ref.artifact_id)
    checks = BrowserChecks({"tenant-a": {"release-7": {**CHECK, "enabled": False}}})
    assert not checks.available("tenant-a") and checks.listing("tenant-a")[0]["status"] == "disabled"
