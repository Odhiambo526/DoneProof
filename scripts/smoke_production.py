from __future__ import annotations

import base64
import json
import pathlib
import sys
import tomllib
import urllib.error
import urllib.request
from urllib.parse import urljoin

ROOT = pathlib.Path(__file__).resolve().parents[1]
with (ROOT / "pyproject.toml").open("rb") as release_file:
    RELEASE_VERSION = tomllib.load(release_file)["project"]["version"]


def fetch(base: str, path: str) -> tuple[int, bytes, dict[str, str]]:
    url = urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    request = urllib.request.Request(url, headers={"User-Agent": f"doneproof-smoke/{RELEASE_VERSION}"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


def fetch_json(base: str, path: str) -> tuple[int, dict]:
    status, body, _ = fetch(base, path)
    try:
        parsed = json.loads(body.decode())
    except Exception as exc:
        raise AssertionError(f"{path} did not return JSON (HTTP {status})") from exc
    return status, parsed


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: smoke_production.py <base-url>", file=sys.stderr)
        return 2
    base = sys.argv[1]

    status, ready = fetch_json(base, "/ready")
    assert status == 200, f"/ready HTTP {status}: {ready}"
    assert ready == {
        "ready": True,
        "database": "ready",
        "storage_backend": "postgresql",
        "durable_storage": True,
        "environment": "production",
        "warnings": [],
    }, f"unexpected /ready payload: {ready}"

    status, health = fetch_json(base, "/health")
    assert status == 200 and health.get("ok") is True, f"unexpected /health: {status} {health}"
    assert health.get("service") == "doneproof"
    assert health.get("version") == RELEASE_VERSION, (
        f"production version {health.get('version')} != release {RELEASE_VERSION}"
    )

    status, signing = fetch_json(base, "/v1/signing-key")
    assert status == 200 and signing.get("algorithm") == "Ed25519", (
        f"unexpected signing key response: {status} {signing}"
    )
    public_key = base64.b64decode(signing["public_key"], validate=True)
    assert len(public_key) == 32
    assert len(signing.get("key_id", "")) == 16
    assert "Pin this public key" in signing.get("trust_model", "")

    status, _, _ = fetch(base, "/v1/overview")
    assert status == 401, f"protected route without workspace key returned HTTP {status}"
    for path in ("/v1/jobs/vj_smoke", "/v1/jobs/vj_smoke/conditions", "/v1/jobs/vj_smoke/wait"):
        status, _, _ = fetch(base, path)
        assert status == 401, f"verification job route without workspace key returned HTTP {status}"

    status, _, _ = fetch(base, "/v1/connections")
    assert status == 401, f"connection management without administrator key returned HTTP {status}"
    status, connections, headers = fetch(base, "/connections")
    assert status == 200 and b"Connection Settings" in connections
    normalized_headers = {key.lower(): value for key, value in headers.items()}
    assert normalized_headers.get("cache-control") == "no-store"
    assert "script-src 'self';" in normalized_headers.get("content-security-policy", "")
    status, console, _ = fetch(base, "/console")
    assert status == 200 and b'href="/connections"' in console

    status, landing, _ = fetch(base, "/")
    assert status == 200 and b"Agents act" in landing and b"DoneProof" in landing

    status, demo, _ = fetch(base, "/demo")
    assert status == 200, f"/demo returned HTTP {status}"
    assert b"False-success demo" in demo and b"Start 90-second demo" in demo

    print(
        json.dumps(
            {
                "ok": True,
                "version": RELEASE_VERSION,
                "storage_backend": ready["storage_backend"],
                "durable_storage": ready["durable_storage"],
                "environment": ready["environment"],
                "auth_boundary": "401-without-key",
                "signing_key": "valid-ed25519-shape",
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"production smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
