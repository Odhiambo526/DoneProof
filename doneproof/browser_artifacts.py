"""Encrypted, bounded screenshot retention; raw pixels are never API/receipt fields."""
import base64
import hashlib
import time
from uuid import uuid4

from .browser_models import ScreenshotRef
from .connection_crypto import CredentialVault
from .connection_store import ConnectionStore

RETENTION_SECONDS = 7 * 86400
MAX_ARTIFACTS_PER_TENANT = 512
MAX_SCREENSHOT_BYTES = 65536


def migrate(con):
    con.execute("""CREATE TABLE IF NOT EXISTS browser_artifacts (
        tenant_id TEXT NOT NULL, id TEXT NOT NULL, contract_id TEXT NOT NULL, condition_id TEXT NOT NULL,
        ciphertext TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 65536),
        created_at BIGINT NOT NULL, expires_at BIGINT NOT NULL,
        PRIMARY KEY(tenant_id,id)
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_browser_artifacts_expiry ON browser_artifacts(expires_at)")


class BrowserArtifacts(ConnectionStore):
    def __init__(self, store, settings):
        super().__init__(store)
        self.vault = CredentialVault(settings.connection_encryption_keys, settings.connection_active_key)

    def save(self, context, png):
        if not isinstance(png, bytes) or not 8 < len(png) <= MAX_SCREENSHOT_BYTES or not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Screenshot is unavailable or exceeds its budget")
        now = int(time.time())
        row = {"tenant_id": context.tenant_id, "provider": "browser", "id": "bs_" + uuid4().hex}
        checksum = hashlib.sha256(png).hexdigest()
        payload = {"png": base64.b64encode(png).decode(), "contract_id": context.contract_id,
                   "condition_id": context.condition_id, "sha256": checksum}
        encrypted = self.vault.encrypt(row, payload, purpose="browser-screenshot-v1")
        with self.transaction() as con:
            # Serialize retention per tenant so concurrent conditions cannot exceed the bound.
            if self.pg:
                key = int.from_bytes(hashlib.sha256(context.tenant_id.encode()).digest()[:8], "big", signed=True)
                self.execute(con, "SELECT pg_advisory_xact_lock(?)", (key,))
            self.execute(con, "DELETE FROM browser_artifacts WHERE expires_at<=?", (now,))
            self.execute(con, "INSERT INTO browser_artifacts VALUES(?,?,?,?,?,?,?,?,?)",
                         (row["tenant_id"], row["id"], context.contract_id, context.condition_id, encrypted,
                          checksum, len(png), time.time_ns(), now + RETENTION_SECONDS))
            self.execute(con, """DELETE FROM browser_artifacts WHERE tenant_id=? AND id IN (
                SELECT id FROM browser_artifacts WHERE tenant_id=? ORDER BY created_at DESC,id DESC LIMIT -1 OFFSET ?
            )""".replace("LIMIT -1", "LIMIT ALL" if self.pg else "LIMIT -1"),
                         (context.tenant_id, context.tenant_id, MAX_ARTIFACTS_PER_TENANT))
        return ScreenshotRef(artifact_id=row["id"], sha256=checksum, bytes=len(png), expires_at=now + RETENTION_SECONDS)

    def read_for_operator(self, tenant, identifier):
        """Local DB/key access only. This method is deliberately not an HTTP route."""
        with self.transaction() as con:
            row = self._row(self.execute(con, "SELECT * FROM browser_artifacts WHERE tenant_id=? AND id=? AND expires_at>?",
                                         (tenant, identifier, int(time.time()))))
        if row is None:
            return None
        payload = self.vault.decrypt({**row, "provider": "browser"}, row["ciphertext"], purpose="browser-screenshot-v1")
        png = base64.b64decode(payload["png"], validate=True)
        if (hashlib.sha256(png).hexdigest() != row["sha256"] or len(png) != row["size_bytes"]
                or payload["contract_id"] != row["contract_id"] or payload["condition_id"] != row["condition_id"]):
            raise RuntimeError("Screenshot integrity check failed")
        return png

    def purge_expired(self):
        with self.transaction() as con:
            return self.execute(con, "DELETE FROM browser_artifacts WHERE expires_at<=?", (int(time.time()),)).rowcount
