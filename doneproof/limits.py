from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """Small in-process pilot limiter. Production multi-instance deployments should use Redis/API gateway limits."""

    def __init__(self, requests_per_minute: int = 120):
        self.requests_per_minute = max(1, requests_per_minute)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.requests_per_minute:
                retry = max(1, int(60 - (now - q[0])))
                return False, retry
            q.append(now)
            return True, 0
