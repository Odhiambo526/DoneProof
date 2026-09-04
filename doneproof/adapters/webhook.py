from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..store import Store
from .base import ObservationContext, ProviderAdapter, ProviderObservation


class WebhookEvidenceAdapter(ProviderAdapter):
    def __init__(self, store: Store):
        self.store = store

    async def observe(self, selector: dict[str, Any], context: ObservationContext) -> ProviderObservation:
        source = selector.get("source")
        event_type = selector.get("event_type")
        object_id = selector.get("object_id")
        if not isinstance(source, str) or not source or len(source) > 100:
            raise ValueError("selector.source is required")
        if not isinstance(event_type, str) or not event_type or len(event_type) > 160:
            raise ValueError("selector.event_type is required")
        if object_id is not None and (not isinstance(object_id, str) or len(object_id) > 300):
            raise ValueError("selector.object_id must be a string or null")
        raw_after = selector.get("created_after") or context.task_started_at
        occurred_after = datetime.fromisoformat(str(raw_after).replace("Z", "+00:00"))
        if occurred_after.tzinfo is None:
            occurred_after = occurred_after.replace(tzinfo=timezone.utc)
        events = self.store.find_events(context.tenant_id, source, event_type, object_id, occurred_after)
        if not events:
            return ProviderObservation(
                state=None,
                source_url=f"doneproof://webhooks/{source}",
                note="No trusted webhook evidence matched the requested outcome after task start.",
            )
        newest = events[0]
        return ProviderObservation(
            state={
                "event_id": newest["event_id"],
                "source": newest["source"],
                "event_type": newest["event_type"],
                "object_id": newest["object_id"],
                "occurred_at": newest["occurred_at"],
                "payload": newest["payload"],
                "payload_hash": newest["payload_hash"],
            },
            source_url=f"doneproof://webhooks/{source}/{newest['event_id']}",
            note=(
                "Matched trusted webhook evidence."
                if len(events) == 1
                else f"Matched {len(events)} events; using the most recent authoritative event."
            ),
        )
