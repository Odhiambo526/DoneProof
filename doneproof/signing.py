from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from copy import deepcopy

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .config import Settings
from .domain import VerificationReceipt


class ReceiptSigner:
    def __init__(self, settings: Settings):
        if settings.signing_seed_b64:
            seed = base64.b64decode(settings.signing_seed_b64, validate=True)
            if len(seed) != 32:
                raise RuntimeError("DONEPROOF_SIGNING_SEED_B64 must decode to exactly 32 bytes")
        else:
            legacy = settings.legacy_receipt_key or "doneproof-development-signing-key"
            seed = hashlib.sha256(legacy.encode()).digest()
        self._private = Ed25519PrivateKey.from_private_bytes(seed)
        self._public = self._private.public_key()
        self.public_key_bytes = self._public.public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public_key_b64 = base64.b64encode(self.public_key_bytes).decode()
        self.key_id = hashlib.sha256(self.public_key_bytes).hexdigest()[:16]

    @staticmethod
    def _payload(receipt: VerificationReceipt) -> bytes:
        obj = deepcopy(receipt.model_dump(mode="json"))
        obj.pop("signature", None)
        obj.pop("receipt_hash", None)
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    def sign(self, receipt: VerificationReceipt) -> VerificationReceipt:
        receipt.validate_recovery_version()
        receipt.signature_alg = "Ed25519"
        receipt.key_id = self.key_id
        receipt.public_key = self.public_key_b64
        payload = self._payload(receipt)
        receipt.receipt_hash = hashlib.sha256(payload).hexdigest()
        receipt.signature = base64.b64encode(self._private.sign(payload)).decode()
        return receipt

    @classmethod
    def verify(cls, receipt: VerificationReceipt) -> bool:
        try:
            receipt.validate_recovery_version()
            public_bytes = base64.b64decode(receipt.public_key, validate=True)
            if hashlib.sha256(public_bytes).hexdigest()[:16] != receipt.key_id:
                return False
            payload = cls._payload(receipt)
            if hashlib.sha256(payload).hexdigest() != receipt.receipt_hash:
                return False
            Ed25519PublicKey.from_public_bytes(public_bytes).verify(
                base64.b64decode(receipt.signature, validate=True), payload
            )
            return True
        except (ValueError, InvalidSignature, TypeError, binascii.Error):
            return False

    @classmethod
    def verify_trusted(cls, receipt: VerificationReceipt, trusted_public_key_b64: str) -> bool:
        """Verify integrity and require a separately trusted signer key.

        The public key embedded in a receipt is sufficient to check that the
        receipt is internally consistent, but it is not by itself proof that
        DoneProof issued the receipt. Callers making audit or authorization
        decisions should pin the deployment public key out-of-band.
        """
        try:
            embedded = base64.b64decode(receipt.public_key, validate=True)
            trusted = base64.b64decode(trusted_public_key_b64, validate=True)
            if len(embedded) != 32 or len(trusted) != 32:
                return False
            if not hmac.compare_digest(embedded, trusted):
                return False
            return cls.verify(receipt)
        except (ValueError, TypeError, binascii.Error):
            return False
