"""Container-local loop freshness, not a claim about remote worker health."""
import json
import os
import time
from pathlib import Path

from .operations import deployment_revision

MAX_AGE = 120


class WorkerHealth:
    def __init__(self, path):
        self.path = Path(path)
        self.loops = {}

    def update(self, operation):
        self.loops[operation] = time.time()
        value = {"pid": os.getpid(), "revision": deployment_revision(), "loops": self.loops}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(self.path)

    def remove(self):
        self.path.unlink(missing_ok=True)


def healthy(path, now=None):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        current = time.time() if now is None else now
        return all(0 <= current - value["loops"][name] <= MAX_AGE
                   for name in ("tick", "callback_tick", "recovery_tick"))
    except (OSError, ValueError, KeyError, TypeError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if healthy(os.getenv("DONEPROOF_WORKER_HEALTH_FILE", "/tmp/doneproof-worker-health.json")) else 1)
