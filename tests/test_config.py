import base64

from doneproof.config import get_settings
from doneproof.signing import ReceiptSigner


def test_signing_seed_normalizes_environment_whitespace(monkeypatch):
    seed = base64.b64encode(bytes(range(32))).decode()
    wrapped = f"  \n{seed[:20]}\n{seed[20:]}\t  "
    monkeypatch.setenv("DONEPROOF_SIGNING_SEED_B64", wrapped)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.signing_seed_b64 == seed
        assert settings.has_stable_signing_key is True
        signer = ReceiptSigner(settings)
        assert len(base64.b64decode(signer.public_key_b64, validate=True)) == 32
    finally:
        get_settings.cache_clear()
