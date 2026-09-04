import base64

import pytest

from doneproof.config import get_settings
from doneproof.signing import ReceiptSigner


@pytest.mark.parametrize("wrapper", ["", '"', "'", "`"])
def test_signing_seed_normalizes_environment_transport_format(monkeypatch, wrapper):
    seed = base64.b64encode(bytes(range(32))).decode()
    wrapped_seed = f"{wrapper}{seed}{wrapper}" if wrapper else seed
    transported = f"  \n{wrapped_seed[:22]}\n{wrapped_seed[22:]}\t  "
    monkeypatch.setenv("DONEPROOF_SIGNING_SEED_B64", transported)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.signing_seed_b64 == seed
        assert settings.has_stable_signing_key is True
        signer = ReceiptSigner(settings)
        assert len(base64.b64decode(signer.public_key_b64, validate=True)) == 32
    finally:
        get_settings.cache_clear()
