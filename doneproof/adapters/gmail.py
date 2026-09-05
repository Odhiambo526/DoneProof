from __future__ import annotations

from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses
from typing import Any

import httpx

from .. import __version__
from ..config import Settings
from ..http import resilient_get
from .base import ObservationContext, ProviderAdapter, ProviderObservation


class GmailAdapter(ProviderAdapter):
    API = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None, *, response_hooks=None):
        self.settings = settings
        self.transport = transport
        self.response_hooks = response_hooks or []

    def _client(self, token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=False,
            transport=self.transport,
            event_hooks={"response": self.response_hooks},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": f"doneproof/{__version__}",
            },
        )

    async def observe(self, selector: dict[str, Any], context: ObservationContext) -> ProviderObservation:
        token = self.settings.gmail_token_for(context.tenant_id)
        if not token:
            return ProviderObservation(
                state=None,
                note="Gmail is not connected for this workspace.",
                indeterminate=True,
            )
        message_id = selector.get("message_id")
        if message_id:
            if not isinstance(message_id, str) or len(message_id) > 200:
                raise ValueError("selector.message_id must be a valid Gmail message id")
            return await self._fetch_message(token, message_id)
        return await self._discover(token, selector, context)

    async def _fetch_message(self, token: str, message_id: str) -> ProviderObservation:
        url = f"{self.API}/messages/{message_id}"
        async with self._client(token) as client:
            r = await resilient_get(client, url, params={"format": "full"})
        if r.status_code == 404:
            return ProviderObservation(state=None, source_url=url, note="Gmail message was not found.")
        if r.status_code in {401, 403}:
            return ProviderObservation(
                state=None, source_url=url, note="Gmail connection could not access the mailbox.", indeterminate=True
            )
        r.raise_for_status()
        return ProviderObservation(state=self._normalize(r.json()), source_url=url)

    async def _discover(self, token: str, selector: dict[str, Any], context: ObservationContext) -> ProviderObservation:
        created_after = self._parse_time(selector.get("created_after") or context.task_started_at)
        subject = selector.get("subject")
        to = selector.get("to")
        thread_id = selector.get("thread_id")
        location = selector.get("location")
        if not any([subject, to, thread_id]):
            raise ValueError("Gmail discovery requires subject, to, or thread_id")
        if location is not None and location not in {"sent", "draft", "other"}:
            raise ValueError("selector.location must be sent, draft, other, or null")

        q = [f"after:{int(created_after.timestamp())}"]
        if subject:
            escaped = str(subject).replace('"', "")
            q.append(f'subject:"{escaped}"')
        if to:
            q.append(f"to:{to}")
        url = f"{self.API}/messages"
        async with self._client(token) as client:
            r = await resilient_get(client, url, params={"q": " ".join(q), "maxResults": 100})
            if r.status_code in {401, 403}:
                return ProviderObservation(
                    state=None,
                    source_url=url,
                    note="Gmail connection could not search the mailbox.",
                    indeterminate=True,
                )
            r.raise_for_status()
            refs = r.json().get("messages", []) or []
            if r.json().get("nextPageToken") or len(refs) > 100:
                return ProviderObservation(None, source_url=url, indeterminate=True,
                    note="Gmail discovery exceeded its search budget; absence and uniqueness cannot be established.")
            candidates: list[dict[str, Any]] = []
            for ref in refs[:100]:
                mid = ref.get("id")
                if not mid:
                    return ProviderObservation(None, source_url=url, indeterminate=True,
                        note="Gmail discovery returned incomplete resource identifiers.")
                detail = await resilient_get(client, f"{self.API}/messages/{mid}", params={"format": "full"})
                if detail.status_code != 200:
                    return ProviderObservation(None, source_url=url, indeterminate=True,
                        note="Gmail discovery could not read every candidate; absence and uniqueness are unknown.")
                normalized = self._normalize(detail.json())
                if datetime.fromisoformat(normalized["internal_date"].replace("Z", "+00:00")) < created_after:
                    continue
                if subject is not None and normalized.get("subject") != subject:
                    continue
                if to is not None and str(to).lower() not in {x.lower() for x in normalized.get("to", [])}:
                    continue
                if thread_id is not None and normalized.get("thread_id") != thread_id:
                    continue
                if location is not None and normalized.get("location") != location:
                    continue
                candidates.append(normalized)

        if not candidates:
            return ProviderObservation(
                state=None, source_url=url, note="No Gmail message matched the completion constraints after task start."
            )
        if len(candidates) > 1:
            return ProviderObservation(
                state={"candidate_count": len(candidates), "candidates": [self._summary(x) for x in candidates[:20]]},
                source_url=url,
                note=f"Gmail discovery matched {len(candidates)} messages; refusing to guess which one belongs to the task.",
                indeterminate=True,
            )
        return ProviderObservation(
            state=candidates[0],
            source_url=f"{self.API}/messages/{candidates[0]['message_id']}",
            note="Discovered one matching Gmail message after task start.",
        )

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise ValueError("created_after must be ISO-8601")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _decode_header_value(value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    @classmethod
    def _normalize(cls, data: dict[str, Any]) -> dict[str, Any]:
        headers = {
            str(x.get("name", "")).lower(): str(x.get("value", ""))
            for x in (data.get("payload") or {}).get("headers", [])
        }
        label_ids = set(data.get("labelIds") or [])
        location = "sent" if "SENT" in label_ids else "draft" if "DRAFT" in label_ids else "other"
        internal_ms = int(data.get("internalDate") or 0)
        internal_date = datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")

        def addresses(name: str) -> list[str]:
            return [addr for _, addr in getaddresses([headers.get(name, "")]) if addr]

        return {
            "message_id": data.get("id"),
            "thread_id": data.get("threadId"),
            "location": location,
            "subject": cls._decode_header_value(headers.get("subject")),
            "from": addresses("from"),
            "to": addresses("to"),
            "cc": addresses("cc"),
            "bcc": addresses("bcc"),
            "internal_date": internal_date,
            "attachment_names": cls._attachments(data.get("payload") or {}),
        }

    @classmethod
    def _attachments(cls, part: dict[str, Any]) -> list[str]:
        names: list[str] = []
        filename = part.get("filename")
        if filename:
            names.append(str(filename))
        for child in part.get("parts") or []:
            names.extend(cls._attachments(child))
        return names

    @staticmethod
    def _summary(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "message_id": data.get("message_id"),
            "thread_id": data.get("thread_id"),
            "location": data.get("location"),
            "subject": data.get("subject"),
            "to": data.get("to"),
            "internal_date": data.get("internal_date"),
        }


def provider_definition():
    from .builtin_provider import definition, gmail_settings
    return definition({
        "provider_id": "gmail", "display_name": "Gmail", "resource_types": ("message",),
        "description": "Sent-vs-draft, recipients, subject, thread and attachment metadata.",
        "discovery": {"supported": True, "identity_field": "message_id", "identity_schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,200}$"},
                      "boundary_field": "created_after"},
        "authentication": {"mode": "managed_oauth", "requirements": ("https://www.googleapis.com/auth/gmail.readonly",),
                           "refresh_required": True, "authorization_origin": "https://accounts.google.com", "onboarding_order": 0},
        "rate_limit": {"concurrency": 4, "preflight_concurrency": 2, "attempts": 4, "base_seconds": 1.0, "cap_seconds": 32.0},
        "evidence_sensitivity": "restricted",
        "compiler_instructions": "Gmail discovery requires BOTH exact subject and recipient; never filter by location. Send requires location=sent, subject equality and recipient containment. No message body, read receipts or business satisfaction evidence.",
    }, lambda runtime: GmailAdapter(gmail_settings(runtime), transport=runtime.transport, response_hooks=list(runtime.response_hooks)))
