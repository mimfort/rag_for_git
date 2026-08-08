"""Провайдер Jira Cloud REST API v3.

Поддерживается только прямой site URL и Basic auth ``email:unscoped-api-token``.
Jira Server/Data Center, OAuth и scoped-token gateway намеренно не поддерживаются.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import httpx

from reviewer.tasks.boards.adf import (
    adf_contains_link,
    adf_to_markdown,
    append_link_paragraph,
    markdown_to_adf,
)
from reviewer.tasks.boards.attachments import (
    attachment_supported,
    fetch_attachment,
)
from reviewer.tasks.boards.base import RawTask, TaskListing, TaskListingStats, project_prefix
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.http import BoardHttpClient
from reviewer.config.provider_access import ProviderAccessSpec
from reviewer.tasks.boards.registry import (
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderOptionSpec,
    ProviderSetupSpec,
)

if TYPE_CHECKING:
    from reviewer.config.task_board import TaskSyncFilter

_PAGE = 100
_ATLASSIAN_TENANT_HOST = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.atlassian\.net$"
)


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
            CredentialFieldSpec("JIRA_EMAIL", "Atlassian account email", secret=True),
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
            label="Jira Cloud",
            help_url="https://id.atlassian.com/manage-profile/security/api-tokens",
            help_text=(
                "Создайте API token без scopes для прямого Jira Cloud site URL, "
                "задайте понятные name/expiration и сразу сохраните token: повторно "
                "его посмотреть нельзя. Пароль Atlassian не подходит."
            ),
            access=ProviderAccessSpec(
                minimum_permissions=(
                    "Browse Projects, Create Issues, Edit Issues и Transition Issues"
                ),
                read_operations=("проекты, задачи, поля, переходы и вложения",),
                write_operations=("создание, правка и перевод задач, добавление PR-ссылок",),
                validation="identity, проект и доступные Jira permissions",
            ),
        ),
        create_target_label="Статус создания",
        done_target_label="Статус завершения",
    )


def _normalize_site_url(value: str, *, secrets: tuple[str, ...]) -> str:
    """Fail-closed Jira Cloud tenant origin до создания authenticated client."""
    try:
        parsed = urlsplit((value or "").strip())
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        parsed = urlsplit("")
        hostname = ""
        port = None
    if (
        parsed.scheme != "https"
        or _ATLASSIAN_TENANT_HOST.fullmatch(hostname) is None
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise BoardProviderError(
            "configuration",
            "Jira base URL must be an exact Jira Cloud tenant origin.",
            hint="Use https://<tenant>.atlassian.net without /rest/api/3.",
            secrets=secrets,
        )
    return f"https://{hostname}"


def _timestamp(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return int(datetime.fromisoformat(normalized).timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        return None


def _https_origin(value: object) -> tuple[str, int] | None:
    """Нормализованный HTTPS origin без userinfo; malformed URL → None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return hostname.lower(), port or 443


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
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._secrets = (api_token, email)
        self._base = _normalize_site_url(base_url, secrets=self._secrets)
        self._key_pattern = key_pattern
        self._issue_type = issue_type
        self._att_origin = _https_origin(self._base)
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
        self._http = BoardHttpClient(
            self._client,
            sleeper=sleeper,
            secrets=self._secrets,
        )

    def close(self) -> None:
        self._http.close()

    def _read(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._http.request_json(method, path, operation="read", **kwargs)

    def _write(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._http.request_json(method, path, operation="write", **kwargs)

    def _project_permissions(self, project: str) -> tuple[dict[str, bool], list[str]]:
        payload = self._read(
            "GET",
            "/rest/api/3/mypermissions",
            params={
                "projectKey": project,
                "permissions": "BROWSE_PROJECTS,CREATE_ISSUES,TRANSITION_ISSUES",
            },
        ) or {}
        permissions = payload.get("permissions") or {}
        capabilities = {
            "read": bool((permissions.get("BROWSE_PROJECTS") or {}).get("havePermission")),
            "create": bool((permissions.get("CREATE_ISSUES") or {}).get("havePermission")),
            "transition": bool(
                (permissions.get("TRANSITION_ISSUES") or {}).get("havePermission")
            ),
        }
        warnings = [
            f"missing Jira permission: {permission}"
            for permission, capability in (
                ("BROWSE_PROJECTS", "read"),
                ("CREATE_ISSUES", "create"),
                ("TRANSITION_ISSUES", "transition"),
            )
            if not capabilities[capability]
        ]
        return capabilities, warnings

    def validate_connection(self, project: str | None = None) -> dict:
        """Проверить identity, видимость проекта и независимые lifecycle permissions."""
        try:
            identity = self._read("GET", "/rest/api/3/myself") or {}
        except BoardProviderError as error:
            if error.category == "authentication":
                raise BoardProviderError(
                    "authentication",
                    error.message,
                    hint=(
                        "Для direct Jira Cloud site URL создайте API token без scopes "
                        "и используйте email Atlassian account."
                    ),
                    secrets=self._secrets,
                ) from None
            raise
        capabilities = {"read": True}
        warnings: list[str] = []
        if project:
            self._read("GET", f"/rest/api/3/project/{quote(project, safe='')}")
            capabilities, warnings = self._project_permissions(project)
        else:
            warnings.append(
                "Jira project was not checked; create and transition permissions are unknown."
            )
        return {
            "status": "ok",
            "identity": {
                "account_id": identity.get("accountId"),
                "display_name": identity.get("displayName"),
            },
            "project": project,
            "capabilities": capabilities,
            "warnings": warnings,
        }

    def list_targets(self, project: str | None) -> dict:
        """Статусы проекта и явные issue types в общей discovery-модели."""
        warnings: list[str] = []
        payload: list[dict] = []
        if project:
            capabilities, warnings = self._project_permissions(project)
            if capabilities["read"]:
                payload = self._read(
                    "GET",
                    f"/rest/api/3/project/{quote(project, safe='')}/statuses",
                ) or []
        if not project:
            warnings.append("Jira project is required for target discovery.")
        statuses: list[dict] = []
        seen_statuses: set[str] = set()
        issue_types: list[dict] = []
        seen_types: set[str] = set()
        for group in payload:
            type_id = str(group.get("id") or "")
            if type_id and type_id not in seen_types and not group.get("subtask"):
                issue_types.append({"id": type_id, "label": group.get("name") or type_id})
                seen_types.add(type_id)
            for status in group.get("statuses") or []:
                status_id = str(status.get("id") or "")
                if status_id and status_id not in seen_statuses:
                    statuses.append(
                        {
                            "id": status_id,
                            "label": status.get("name") or status_id,
                            "purposes": ["create", "done"],
                        }
                    )
                    seen_statuses.add(status_id)
        return {
            "targets": statuses,
            "options": [
                {
                    "key": "issue_type",
                    "label": "Issue type",
                    "required_for": ["create"],
                    "choices": issue_types,
                }
            ],
            "warnings": warnings,
        }

    def _transitions(self, key: str) -> list[dict]:
        payload = self._read(
            "GET",
            f"/rest/api/3/issue/{quote(key, safe='')}/transitions",
        ) or {}
        return list(payload.get("transitions") or [])

    def _resolve_issue_type(self, project: str) -> str:
        """Resolve configured Jira issue type to an exact project-scoped ID."""
        assert self._issue_type is not None
        if self._issue_type.isdigit():
            return self._issue_type
        payload = self._read(
            "GET",
            f"/rest/api/3/project/{quote(project, safe='')}/statuses",
        ) or []
        issue_types = [
            {
                "id": str(item.get("id") or ""),
                "label": str(item.get("name") or ""),
            }
            for item in payload
            if item.get("id") and not item.get("subtask")
        ]
        exact_id = [item for item in issue_types if item["id"] == self._issue_type]
        if exact_id:
            return exact_id[0]["id"]
        by_label = [item for item in issue_types if item["label"] == self._issue_type]
        if len(by_label) == 1:
            return by_label[0]["id"]
        if len(by_label) > 1:
            raise BoardProviderError(
                "configuration",
                "Configured Jira issue_type label is ambiguous.",
                hint="Select the exact issue type ID returned by target discovery.",
            )
        raise BoardProviderError(
            "configuration",
            "Configured Jira issue_type is unavailable for this project.",
            hint="Select an issue type returned by target discovery.",
        )

    @staticmethod
    def _resolve_transition(
        transitions: list[dict],
        target: str,
    ) -> tuple[dict | None, list[str]]:
        by_id = [
            item
            for item in transitions
            if str((item.get("to") or {}).get("id") or "") == target
        ]
        if by_id:
            return by_id[0], []
        by_label = [
            item
            for item in transitions
            if str((item.get("to") or {}).get("name") or "") == target
        ]
        if len(by_label) == 1:
            return by_label[0], []
        if len(by_label) > 1:
            return None, [f"Jira target {target!r} is ambiguous; transition was not applied."]
        return None, [f"Jira target {target!r} is unavailable; transition was not applied."]

    def _apply_transition(
        self,
        key: str,
        target: str,
    ) -> tuple[str | None, list[str]]:
        try:
            transitions = self._transitions(key)
        except BoardProviderError as error:
            return None, [f"Jira transitions are unavailable: {error.category}."]
        transition, warnings = self._resolve_transition(transitions, target)
        if transition is None:
            return None, warnings
        status = transition.get("to") or {}
        try:
            self._write(
                "POST",
                f"/rest/api/3/issue/{quote(key, safe='')}/transitions",
                json={"transition": {"id": str(transition.get("id") or "")}},
            )
        except BoardProviderError as error:
            warnings.append(f"Jira transition failed: {error.category}.")
            return None, warnings
        return str(status.get("id") or status.get("name") or target), warnings

    def create(
        self,
        doc_md: str,
        *,
        title: str,
        target: str | None,
        project: str | None,
    ) -> dict:
        """Создать issue ровно один раз и отдельно, fail-soft, применить transition."""
        if not project:
            raise BoardProviderError(
                "configuration",
                "Jira project is required to create an issue.",
            )
        if not self._issue_type:
            raise BoardProviderError(
                "configuration",
                "Jira issue_type is required to create an issue.",
                hint="Select an issue type from Jira target discovery.",
            )
        issue_type_id = self._resolve_issue_type(project)
        description = markdown_to_adf(doc_md)
        created = self._write(
            "POST",
            "/rest/api/3/issue",
            json={
                "fields": {
                    "project": {"key": project},
                    "summary": title,
                    "description": description.value,
                    "issuetype": {"id": issue_type_id},
                }
            },
        ) or {}
        key = str(created.get("key") or "")
        if not key:
            raise BoardProviderError(
                "unsupported",
                "Jira create response did not include an issue key.",
            )
        warnings = list(description.warnings)
        target_resolved = None
        if target:
            target_resolved, transition_warnings = self._apply_transition(key, target)
            warnings.extend(transition_warnings)
        return {
            "key": key,
            "url": f"{self._base}/browse/{key}",
            "board_id": str(created.get("id") or key),
            "target_resolved": target_resolved,
            "warnings": warnings,
        }

    def finish(
        self,
        key: str,
        pr_url: str,
        *,
        note: str | None = None,
        mark_done: bool = True,
        target: str | None = None,
    ) -> dict:
        """Независимо обновить ADF description и применить доступный transition."""
        safe_key = quote(key, safe="")
        issue = self._read(
            "GET",
            f"/rest/api/3/issue/{safe_key}",
            params={"fields": "description,status"},
        ) or {}
        fields = issue.get("fields") or {}
        description = fields.get("description")
        document = description if isinstance(description, Mapping) else None
        link_present = bool(pr_url and adf_contains_link(document, pr_url))
        pr_link_added = False
        warnings: list[str] = []
        if pr_url and not link_present:
            updated = append_link_paragraph(
                document,
                pr_url,
                label=f"PR: {pr_url}",
                note=note,
            )
            try:
                self._write(
                    "PUT",
                    f"/rest/api/3/issue/{safe_key}",
                    json={"fields": {"description": updated}},
                )
                pr_link_added = True
            except BoardProviderError as error:
                warnings.append(f"Jira description update failed: {error.category}.")

        status = fields.get("status") or {}
        current_id = str(status.get("id") or "")
        current_name = str(status.get("name") or "")
        already_at_target = bool(
            target and (current_id == target or current_name == target)
        )
        done_set = False
        if mark_done and not already_at_target:
            if not target:
                warnings.append(
                    "Jira done target is required; localized status was not guessed."
                )
            else:
                resolved, transition_warnings = self._apply_transition(key, target)
                done_set = resolved is not None
                warnings.extend(transition_warnings)

        link_satisfied = not pr_url or link_present or pr_link_added
        done_satisfied = not mark_done or already_at_target or done_set
        return {
            "key": key,
            "board_id": str(issue.get("id") or key),
            "done_set": done_set,
            "pr_link_added": pr_link_added,
            "already_closed": (
                not pr_link_added
                and not done_set
                and link_satisfied
                and done_satisfied
            ),
            "warnings": warnings,
        }

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
            archived=None,
            terminal=None,
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

    def _iter_raw_rows(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        count = 0
        if limit is not None and count >= limit:
            return
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

    def iter_raw(
        self,
        board: str | None,
        limit: int | None,
        *,
        sync_filter: TaskSyncFilter | None = None,
        now_ms: int | None = None,
    ) -> TaskListing:
        if limit == 0:
            return TaskListing(rows=iter(()))
        return TaskListing(
            rows=self._iter_raw_rows(board, limit),
            stats=TaskListingStats(),
        )

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
                if _https_origin(url) != self._att_origin:
                    warnings.append(f"вложение {name!r}: origin не разрешён")
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
