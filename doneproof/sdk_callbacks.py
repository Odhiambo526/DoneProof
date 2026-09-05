"""Verify raw completion callbacks before parsing or acting on their contents."""
import hashlib
import hmac
import math
import re
import threading
import time
from typing import Mapping, Protocol

from .assurance_models import CompletionEvent
from .sdk_common import CallbackError, DuplicateEvent


class ReplayStore(Protocol):
    def claim(self, event_id: str, expires_at: float) -> bool:
        """Atomically insert if absent. Production implementations must be durable/shared."""
        ...


class MemoryReplayStore:
    """Single-process development receiver only; restart loses deduplication state."""
    def __init__(self, capacity=10000):
        self._lock, self._seen, self.capacity = threading.Lock(), {}, capacity

    def claim(self, event_id: str, expires_at: float) -> bool:
        with self._lock:
            now = time.time()
            self._seen = {key: expiry for key, expiry in self._seen.items() if expiry > now}
            if event_id in self._seen:
                return False
            if len(self._seen) >= self.capacity:
                raise CallbackError('deduplication_capacity_exceeded')
            self._seen[event_id] = expires_at
            return True


def verify_callback(body: bytes, headers: Mapping[str, str], *, secret: str,
                    replay_store: ReplayStore, max_skew_seconds: int = 300, now: float | None = None) -> CompletionEvent:
    now = time.time() if now is None else now
    if not isinstance(body, bytes) or len(body) > 65536 or len(secret) < 32 or not 1 <= max_skew_seconds <= 600 or not math.isfinite(now):
        raise CallbackError('invalid_callback_configuration_or_size')
    fields = {key.lower(): value for key, value in headers.items()}
    timestamp = fields.get('x-doneproof-timestamp', '')
    signature = fields.get('x-doneproof-signature', '')
    if (not re.fullmatch(r'[0-9]{1,12}', timestamp) or abs(now - int(timestamp)) > max_skew_seconds
            or not re.fullmatch(r'sha256=[a-f0-9]{64}', signature)):
        raise CallbackError('invalid_callback_authentication')
    expected = hmac.new(secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature[7:]):
        raise CallbackError('invalid_callback_authentication')
    try:
        event = CompletionEvent.model_validate_json(body)
        if fields.get('x-doneproof-event') != event.event_id or event.finished_at > now + max_skew_seconds:
            raise ValueError
    except ValueError:
        raise CallbackError('invalid_callback_payload') from None
    # Retain longer than the server's 24-hour delivery retry horizon.
    try:
        claimed = replay_store.claim(event.event_id, now + 86400 + max_skew_seconds)
    except Exception:
        raise CallbackError('receiver_deduplication_unavailable') from None
    if not claimed:
        raise DuplicateEvent('callback_already_received')
    return event
