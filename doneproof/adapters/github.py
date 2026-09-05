from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from .. import __version__
from ..http import resilient_get
from .base import ObservationContext, ProviderAdapter, ProviderObservation

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MAX_DISCOVERY_PAGES = 5


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("selector.created_after must be ISO-8601") from exc
    else:
        raise ValueError("selector.created_after must be ISO-8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class GitHubAdapter(ProviderAdapter):
    API = "https://api.github.com"

    def __init__(self, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None, *, allow_env=True, response_hooks=None):
        self.token = token or (os.getenv("GITHUB_TOKEN") if allow_env else None)
        self.transport = transport
        self.response_hooks = response_hooks or []

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"doneproof/{__version__}",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=12.0,
            follow_redirects=False,
            transport=self.transport,
            headers=self._headers(),
            event_hooks={"response": self.response_hooks},
        )

    async def observe(self, selector: dict[str, Any], context: ObservationContext) -> ProviderObservation:
        repo = str(selector.get("repo", ""))
        kind = selector.get("kind")
        number = selector.get("number")
        if not _REPO.fullmatch(repo):
            raise ValueError("selector.repo must be 'owner/repo'")
        if kind not in {"issue", "pull_request"}:
            raise ValueError("selector.kind must be issue or pull_request")

        if number is not None:
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise ValueError("selector.number must be a positive integer or null")
            return await self._observe_number(repo, kind, number)

        return await self._discover(repo, kind, selector)

    async def _observe_number(self, repo: str, kind: str, number: int) -> ProviderObservation:
        path = "issues" if kind == "issue" else "pulls"
        url = f"{self.API}/repos/{repo}/{path}/{number}"
        async with self._client() as client:
            r = await resilient_get(client, url)
        if r.status_code == 404:
            return ProviderObservation(
                state=None,
                source_url=url,
                note="GitHub returned 404; absence cannot be distinguished from inaccessible private state.",
                indeterminate=True,
            )
        r.raise_for_status()
        data = r.json()
        normalized = self._normalize(kind, data)
        return ProviderObservation(state=normalized, source_url=data.get("html_url") or url)

    async def _discover(self, repo: str, kind: str, selector: dict[str, Any]) -> ProviderObservation:
        created_after = _parse_time(selector.get("created_after"))
        title = selector.get("title")
        author = selector.get("author")
        head_ref = selector.get("head_ref")

        if not created_after:
            raise ValueError("discovery selector.created_after is required when number is null")
        if title is None and author is None and not (kind == "pull_request" and head_ref is not None):
            raise ValueError("discovery requires at least one of title, author, or pull-request head_ref")
        if head_ref is not None and kind != "pull_request":
            raise ValueError("selector.head_ref is only valid for pull requests")

        path = "issues" if kind == "issue" else "pulls"
        url = f"{self.API}/repos/{repo}/{path}"
        candidates: list[dict[str, Any]] = []

        async with self._client() as client:
            complete = False
            malformed = False
            for page in range(1, _MAX_DISCOVERY_PAGES + 1):
                params: dict[str, Any] = {
                    "state": "all",
                    "sort": "created",
                    "direction": "desc",
                    "per_page": 100,
                    "page": page,
                }
                # Issues support a server-side `since` bound. Pull requests do
                # not, so both paths are still bounded again client-side.
                if kind == "issue":
                    params["since"] = created_after.isoformat().replace("+00:00", "Z")

                r = await resilient_get(client, url, params=params)
                if r.status_code == 404:
                    return ProviderObservation(
                        state=None,
                        source_url=url,
                        note="GitHub returned 404 while discovering resources; repository may be inaccessible.",
                        indeterminate=True,
                    )
                r.raise_for_status()
                items = r.json()
                if not isinstance(items, list):
                    raise ValueError("GitHub discovery response was not a list")

                reached_time_bound = False
                for item in items:
                    # GitHub's issues endpoint includes pull requests.
                    if kind == "issue" and item.get("pull_request") is not None:
                        continue
                    created_at = _parse_time(item.get("created_at"))
                    if created_at is None:
                        malformed = True
                        continue
                    if created_at < created_after:
                        reached_time_bound = True
                        continue
                    if title is not None and item.get("title") != title:
                        continue
                    if author is not None and (item.get("user") or {}).get("login") != author:
                        continue
                    if (
                        kind == "pull_request"
                        and head_ref is not None
                        and (item.get("head") or {}).get("ref") != head_ref
                    ):
                        continue
                    candidates.append(item)

                if len(items) < 100 or reached_time_bound:
                    complete = True
                    break

        if not complete or malformed:
            return ProviderObservation(None, source_url=url, indeterminate=True,
                note="GitHub discovery was incomplete; absence and uniqueness cannot be established.")
        if not candidates:
            return ProviderObservation(
                state=None,
                source_url=url,
                note="No GitHub resource matched the discovery constraints after task start.",
            )

        if len(candidates) > 1:
            summaries = [self._candidate_summary(kind, x) for x in candidates[:20]]
            return ProviderObservation(
                state={"candidate_count": len(candidates), "candidates": summaries},
                source_url=url,
                note=f"Discovery matched {len(candidates)} candidates; refusing to guess which resource belongs to the task.",
                indeterminate=True,
            )

        number = candidates[0].get("number")
        if not isinstance(number, int) or number < 1:
            raise ValueError("discovered GitHub resource had no valid number")
        result = await self._observe_number(repo, kind, number)
        if result.note:
            result.note = f"Discovered #{number}. {result.note}"
        else:
            result.note = f"Discovered unique matching {kind} #{number} after task start."
        return result

    @staticmethod
    def _candidate_summary(kind: str, data: dict[str, Any]) -> dict[str, Any]:
        out = {
            "number": data.get("number"),
            "title": data.get("title"),
            "author": (data.get("user") or {}).get("login"),
            "created_at": data.get("created_at"),
            "url": data.get("html_url"),
        }
        if kind == "pull_request":
            out["head_ref"] = (data.get("head") or {}).get("ref")
        return out

    @staticmethod
    def _normalize(kind: str, data: dict[str, Any]) -> dict[str, Any]:
        out = {
            "number": data.get("number"),
            "title": data.get("title"),
            "body": data.get("body"),
            "state": data.get("state"),
            "locked": data.get("locked"),
            "author": (data.get("user") or {}).get("login"),
            "assignees": [x.get("login") for x in data.get("assignees", [])],
            "labels": [x.get("name") for x in data.get("labels", [])],
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "closed_at": data.get("closed_at"),
        }
        if kind == "pull_request":
            out.update(
                {
                    "draft": data.get("draft"),
                    "merged": data.get("merged"),
                    "mergeable": data.get("mergeable"),
                    "head_ref": (data.get("head") or {}).get("ref"),
                    "base_ref": (data.get("base") or {}).get("ref"),
                }
            )
        return out


def provider_definition():
    from .builtin_provider import definition
    return definition({
        "provider_id": "github", "display_name": "GitHub", "resource_types": ("issue", "pull_request"),
        "description": "Issues and pull requests with time-bounded resource discovery. Public anonymous reads are supported when no connection exists.",
        "discovery": {"supported": True, "identity_field": "number", "identity_schema": {"type": "integer", "minimum": 1, "maximum": 2**53-1},
                      "scope_fields": ("repo", "kind"), "boundary_field": "created_after"},
        "authentication": {"mode": "managed_oauth", "requirements": ("Read-only GitHub App issues and pull_requests permissions on installed repositories",),
                           "public_read": True, "authorization_origin": "https://github.com", "onboarding_order": 1},
        "rate_limit": {"concurrency": 8, "preflight_concurrency": 4, "attempts": 4, "base_seconds": 1.0, "cap_seconds": 60.0},
        "evidence_sensitivity": "confidential",
        "compiler_instructions": "GitHub discovery requires exact title, repo and kind. Existing issue/PR mutations require transitions. No review approval or code correctness evidence.",
    }, lambda runtime: GitHubAdapter(token=runtime.credentials["access_token"] if runtime.credentials else None,
            allow_env=False, transport=runtime.transport, response_hooks=list(runtime.response_hooks)))
