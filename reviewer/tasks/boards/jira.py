"""Провайдер Jira Cloud REST API v3.

Поддерживается только прямой site URL и Basic auth ``email:unscoped-api-token``.
Jira Server/Data Center, OAuth и scoped-token gateway намеренно не поддерживаются.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from reviewer.tasks.boards.adf import adf_to_markdown
from reviewer.tasks.boards.attachments import (
    attachment_supported,
    fetch_attachment,
    host_allowed,
)
from reviewer.tasks.boards.base import RawTask, project_prefix
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.http import BoardHttpClient
from reviewer.tasks.boards.registry import (
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderOptionSpec,
    ProviderSetupSpec,
)

_PAGE = 100


def _build_provider(context: ProviderBuildContext) -> JiraCloudBoard:
    issue_type = context.options.get("issue_type")
    return JiraCloudBoard(
        base_url=context.credentials["JIRA_BASE_URL"],
        email=context.credentials["JIRA_EMAIL"],
        api_token=context.credentials["JIRA_API_TOKEN"],
        key_pattern=context.key_pattern,
        issue_type=str(issue_type) if issue_type else None,
        attachment_max_bytes=context.attachment_max_bytes,
        attachment_timeout=context.attachment_timeout,
        attachment_store_chars=context.attachment_store_chars,
    )


def provider_spec() -> BoardProviderSpec:
    """Registry metadata Jira; production-регистрация включается только с полным lifecycle."""
    return BoardProviderSpec(
        board_type="jira",
        factory=_build_provider,
        credential_fields=(
            CredentialFieldSpec("JIRA_BASE_URL", "Jira Cloud site URL"),
            CredentialFieldSpec("JIRA_EMAIL", "Atlassian account email"),
            CredentialFieldSpec("JIRA_API_TOKEN", "Atlassian API token", secret=True),
        ),
        option_fields=(
            ProviderOptionSpec(
                "issue_type",
                "Issue type",
                required_for=("create",),
            ),
        ),
        setup=ProviderSetupSpec(
            "Jira Cloud",
            "https://id.atlassian.com/manage-profile/security/api-tokens",
            "Создайте API token без scopes для прямого Jira Cloud site URL.",
        ),
        create_target_label="Статус создания",
        done_target_label="Статус завершения",
    )


def _site_url(value: str, *, secrets: tuple[str, ...]) -> str:
    parsed = urlsplit((value or "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise BoardProviderError(
            "configuration",
            "Jira base URL must be a direct HTTPS site URL.",
            hint="Use https://<site> without /rest/api/3.",
            secrets=secrets,
        )
    return f"https://{parsed.netloc}"


def _timestamp(value: object) -> int:
    if not isinstance(value, str) or not value:
        return 0
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return int(datetime.fromisoformat(normalized).timestamp() * 1000)


def _issue_links(issue: Mapping[str, Any]) -> list[dict]:
    result: list[dict] = []
    fields = issue.get("fields") if isinstance(issue.get("fields"), Mapping) else {}
    for link in fields.get("issuelinks") or []:
        relation = link.get("type") or {}
        if isinstance(link.get("outwardIssue"), Mapping):
            linked = link["outwardIssue"]
            label = relation.get("outward") or relation.get("name") or "related"
        elif isinstance(link.get("inwardIssue"), Mapping):
            linked = link["inwardIssue"]
            label = relation.get("inward") or relation.get("name") or "related"
        else:
            continue
        key = linked.get("key")
        if not key:
            continue
        linked_fields = linked.get("fields") or {}
        result.append(
            {
                "type": str(label).lower(),
                "key": key,
                "title": linked_fields.get("summary") or "",
            }
        )
    return result


def _subtasks(issue: Mapping[str, Any]) -> list[dict]:
    fields = issue.get("fields") if isinstance(issue.get("fields"), Mapping) else {}
    result: list[dict] = []
    for item in fields.get("subtasks") or []:
        key = item.get("key")
        if key:
            result.append(
                {
                    "key": key,
                    "title": (item.get("fields") or {}).get("summary") or "",
                }
            )
    return result


def _attachments(issue: Mapping[str, Any]) -> list[dict]:
    fields = issue.get("fields") if isinstance(issue.get("fields"), Mapping) else {}
    return [
        {
            "name": item.get("filename") or "",
            "mime": item.get("mimeType"),
            "size": item.get("size"),
            "url": item.get("content") or "",
        }
        for item in fields.get("attachment") or []
        if item.get("content")
    ]


class JiraCloudBoard:
    """Jira Cloud adapter поверх общего безопасного board HTTP client."""

    board_type = "jira"
    _FIELDS = (
        "summary,description,status,updated,subtasks,issuelinks,"
        "attachment,issuetype,project"
    )

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        key_pattern: str,
        issue_type: str | None,
        attachment_max_bytes: int,
        attachment_timeout: float,
        attachment_store_chars: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._secrets = (api_token, email)
        self._base = _site_url(base_url, secrets=self._secrets)
        self._key_pattern = key_pattern
        self._issue_type = issue_type
        self._att_domains = (urlsplit(self._base).hostname or "",)
        self._att_max_bytes = attachment_max_bytes
        self._att_timeout = attachment_timeout
        self._att_store_chars = attachment_store_chars
        self._client = httpx.Client(
            base_url=self._base,
            auth=httpx.BasicAuth(email, api_token),
            headers={"Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )
        self._http = BoardHttpClient(self._client, secrets=self._secrets)

    def close(self) -> None:
        self._http.close()

    def _read(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._http.request_json(method, path, operation="read", **kwargs)

    @staticmethod
    def _raw_from_issue(issue: Mapping[str, Any]) -> RawTask:
        fields = issue.get("fields") if isinstance(issue.get("fields"), Mapping) else {}
        description = fields.get("description")
        converted = adf_to_markdown(description if isinstance(description, Mapping) else None)
        key = str(issue.get("key") or "")
        project = fields.get("project") if isinstance(fields.get("project"), Mapping) else {}
        issue_type = (
            fields.get("issuetype") if isinstance(fields.get("issuetype"), Mapping) else {}
        )
        status = fields.get("status") if isinstance(fields.get("status"), Mapping) else {}
        subtasks = _subtasks(issue)
        return RawTask(
            key=key,
            project_code=key,
            title=str(fields.get("summary") or ""),
            description=str(converted.value),
            status=status.get("name"),
            subtask_ids=[item["key"] for item in subtasks],
            timestamp=_timestamp(fields.get("updated")),
            links=_issue_links(issue),
            attachments=_attachments(issue),
            board_id=str(issue.get("id") or key),
            provider_data={
                "warnings": list(converted.warnings),
                "subtasks": subtasks,
                "issue_type": {"id": issue_type.get("id"), "name": issue_type.get("name")},
                "project": {
                    "id": project.get("id"),
                    "key": project.get("key"),
                    "name": project.get("name"),
                },
            },
        )

    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        count = 0
        token: str | None = None
        while True:
            payload: dict[str, Any] = {
                "jql": (
                    f'project = "{board.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}" '
                    "ORDER BY updated ASC"
                    if board
                    else "ORDER BY updated ASC"
                ),
                "fields": self._FIELDS.split(","),
                "maxResults": _PAGE,
            }
            if token:
                payload["nextPageToken"] = token
            page = self._read("POST", "/rest/api/3/search/jql", json=payload) or {}
            for issue in page.get("issues") or []:
                yield self._raw_from_issue(issue)
                count += 1
                if limit is not None and count >= limit:
                    return
            token = page.get("nextPageToken")
            if not token or page.get("isLast") is True:
                return

    def fetch_one(self, key: str) -> RawTask | None:
        try:
            issue = self._read(
                "GET",
                f"/rest/api/3/issue/{quote(key, safe='')}",
                params={"fields": self._FIELDS},
            )
        except BoardProviderError as error:
            if error.category == "not_found":
                return None
            raise
        return self._raw_from_issue(issue) if issue else None

    def _brief(self, raw: RawTask, *, with_details: bool) -> dict:
        provider_data = raw.provider_data
        warnings = list(provider_data.get("warnings") or [])
        subtasks = provider_data.get("subtasks") or []
        links = [
            {"type": "subtask", "key": item["key"], "title": item.get("title") or ""}
            for item in subtasks
        ]
        links.extend(raw.links)
        covered = {raw.key, *(item["key"] for item in links)}
        if self._key_pattern:
            for match in re.finditer(self._key_pattern, raw.description):
                key = match.group(0)
                if key not in covered:
                    links.append({"type": "related", "key": key, "title": ""})
                    covered.add(key)

        attachments: list[dict] = []
        if with_details:
            for attachment in raw.attachments:
                name = attachment["name"]
                url = attachment["url"]
                size = attachment.get("size")
                mime = attachment.get("mime")
                if not host_allowed(url, self._att_domains):
                    warnings.append(f"вложение {name!r}: host не разрешён")
                    continue
                if isinstance(size, int) and size > self._att_max_bytes:
                    warnings.append(f"вложение {name!r}: размер превышает лимит")
                    continue
                if not attachment_supported(name, mime):
                    warnings.append(f"вложение {name!r}: формат не поддерживается")
                    continue
                result = fetch_attachment(
                    self._client,
                    name=name,
                    mime=mime,
                    size=size,
                    url=url,
                    timeout=self._att_timeout,
                    max_bytes=self._att_max_bytes,
                    store_chars=self._att_store_chars,
                )
                attachments.append(result)
                if result["content_text"] is None:
                    warnings.append(f"вложение {name!r}: содержимое недоступно")

        return {
            "key": raw.key,
            "aliases": [],
            "title": raw.title,
            "description": raw.description,
            "criteria": [
                item.get("title") or item["key"]
                for item in subtasks
            ] if with_details else [],
            "status": raw.status,
            "url": f"{self._base}/browse/{raw.key}" if raw.key else None,
            "links": links,
            "project": project_prefix(raw.key),
            "attachments": attachments,
            "warnings": warnings,
            "provider_data": {
                "issue_type": provider_data.get("issue_type") or {},
                "project": provider_data.get("project") or {},
            },
        }

    def normalize(self, raw: RawTask) -> dict:
        return self._brief(raw, with_details=True)

    def normalize_meta(self, raw: RawTask) -> dict:
        return self._brief(raw, with_details=False)
