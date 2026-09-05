"""Versioned AES-GCM envelopes bound to the workspace, provider and row."""
from __future__ import annotations

import base64
import json
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CredentialVault:
    def __init__(self, keys, active_key):
        self.keys = {}
        try:
            for key_id, encoded in keys.items():
                if not isinstance(key_id, str) or not key_id or len(key_id) > 64:
                    raise ValueError
                key = base64.b64decode(encoded, validate=True)
                if len(key) != 32:
                    raise ValueError
                self.keys[key_id] = key
            if keys and active_key not in self.keys:
                raise ValueError
            if active_key and not keys:
                raise ValueError
        except Exception:
            raise RuntimeError("Invalid connection encryption key configuration") from None
        self.active_key = active_key

    @property
    def available(self):
        return self.active_key in self.keys

    @staticmethod
    def aad(row, purpose):
        return json.dumps(["doneproof-connections-v1", row["tenant_id"], row["provider"], row["id"], purpose],
                          separators=(",", ":")).encode()

    def encrypt(self, row, payload, purpose="credential"):
        if not self.available:
            raise RuntimeError("Connection encryption is unavailable")
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self.keys[self.active_key]).encrypt(
            nonce, json.dumps(payload, separators=(",", ":")).encode(), self.aad(row, purpose))
        return json.dumps({"v": 1, "kid": self.active_key,
            "data": base64.b64encode(nonce + ciphertext).decode()}, separators=(",", ":"))

    def decrypt(self, row, envelope=None, purpose="credential"):
        try:
            value = json.loads(envelope or row["credential_ciphertext"])
            if value["v"] != 1:
                raise ValueError
            raw = base64.b64decode(value["data"], validate=True)
            plain = AESGCM(self.keys[value["kid"]]).decrypt(raw[:12], raw[12:], self.aad(row, purpose))
            result = json.loads(plain)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except Exception:
            raise RuntimeError("Connection credential is unavailable") from None
