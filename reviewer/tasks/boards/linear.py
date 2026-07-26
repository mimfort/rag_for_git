"""Провайдер доски Linear (GraphQL API, SaaS-only).

Поддерживается только personal API key: заголовок ``Authorization: <API_KEY>``
**без** префикса ``Bearer`` (префикс — только у OAuth access token, а OAuth
loopback здесь намеренно не поддерживается).
Транспорт — общий ``RestBoardBase`` + ``GraphQLClient``; своей httpx-обвязки нет.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from reviewer.tasks.boards.base import RawTask, project_prefix
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.graphql import GraphQLClient
from reviewer.tasks.boards.registry import (
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderOptionSpec,
    ProviderSetupSpec,
)
from reviewer.tasks.boards.restbase import RestBoardBase

DEFAULT_API_BASE = "https://api.linear.app"
_PAGE = 50
_TEAMS_PAGE = 50
_STATES_PAGE = 100
# Вложенные connection'ы множат complexity запроса (лимит Linear — 10000 points
# на запрос, 250000 в час), поэтому sub-issues и вложения берутся с запасом, но
# без 50×50: сверх этого числа списки в брифе усекаются.
_CHILDREN_PAGE = 25
_ATTACHMENTS_PAGE = 25

# Поля issue: description — уже markdown (schema: «The issue's description in
# markdown format»), identifier — человекочитаемый ключ ENG-123, children —
# sub-issues, attachments — метаданные вложений.
_ISSUE_FIELDS = f"""
    id
    identifier
    title
    description
    updatedAt
    url
    state {{ id name type }}
    team {{ id key }}
    children(first: {_CHILDREN_PAGE}) {{ nodes {{ identifier title }} }}
    attachments(first: {_ATTACHMENTS_PAGE}) {{ nodes {{ title url }} }}
"""

_ISSUES_QUERY = f"""
query ReviewerLinearIssues($filter: IssueFilter, $first: Int, $after: String) {{
  issues(filter: $filter, first: $first, after: $after, orderBy: updatedAt) {{
    nodes {{{_ISSUE_FIELDS}    }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}
"""

_ISSUE_QUERY = f"""
query ReviewerLinearIssue($id: String!) {{
  issue(id: $id) {{{_ISSUE_FIELDS}  }}
}}
"""

_TEAMS_QUERY = f"""
query ReviewerLinearTeams($filter: TeamFilter, $first: Int) {{
  teams(filter: $filter, first: $first) {{
    nodes {{
      id
      key
      name
      states(first: {_STATES_PAGE}) {{ nodes {{ id name type position }} }}
    }}
  }}
}}
"""

_ISSUE_FINISH_QUERY = f"""
query ReviewerLinearIssueForFinish($id: String!) {{
  issue(id: $id) {{
    id
    identifier
    description
    state {{ id name type }}
    team {{
      id
      key
      states(first: {_STATES_PAGE}) {{ nodes {{ id name type position }} }}
    }}
  }}
}}
"""

_ISSUE_UPDATE_MUTATION = """
mutation ReviewerLinearIssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue { id identifier state { id name type } }
  }
}
"""

_ISSUE_CREATE_MUTATION = """
mutation ReviewerLinearIssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id identifier url state { id name } }
  }
}
"""

_VIEWER_QUERY = """
query ReviewerLinearViewer {
  viewer { id name displayName }
  organization { id name urlKey }
}
"""

# Терминальные типы workflow state: только они годятся как done-цель.
_DONE_STATE_TYPES = frozenset({"completed", "canceled"})

# Окна лимитов Linear: (счётчик остатка, момент сброса в epoch ms UTC).
_RATE_LIMIT_WINDOWS = (
    ("X-RateLimit-Requests-Remaining", "X-RateLimit-Requests-Reset"),
    ("X-RateLimit-Endpoint-Requests-Remaining", "X-RateLimit-Endpoint-Requests-Reset"),
    ("X-RateLimit-Complexity-Remaining", "X-RateLimit-Complexity-Reset"),
)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Значение заголовка без учёта регистра (в тестах приходит обычный dict)."""
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    return value


def _rate_limit_hint(status: int, headers: Mapping[str, str]) -> float | None:
    """Linear отдаёт превышение лимита как HTTP **400** (не 429).

    Статуса недостаточно: обычный 400 — это невалидный запрос. Признак лимита —
    обнулённый счётчик одного из окон; пауза считается до ``*-Reset`` (epoch ms UTC).
    Возврат None означает «это не rate limit» — категория остаётся прежней.
    """
    if status != 400:
        return None
    for remaining_header, reset_header in _RATE_LIMIT_WINDOWS:
        remaining = _header(headers, remaining_header)
        if remaining is None or remaining.strip() not in {"0", "0.0"}:
            continue
        try:
            return max(0.0, float(_header(headers, reset_header) or 0) / 1000.0 - time.time())
        except (TypeError, ValueError):
            return 1.0
    return None


def _build_provider(context: ProviderBuildContext) -> LinearBoard:
    """Фабрика провайдера из server-side credentials и non-secret options."""
    team_key = context.options.get("team_key")
    return LinearBoard(
        api_key=context.credentials["LINEAR_API_KEY"],
        api_base=context.credentials.get("LINEAR_API_BASE") or DEFAULT_API_BASE,
        team_key=str(team_key) if team_key else "",
        key_pattern=context.key_pattern,
        url_template=context.url_template,
    )


def provider_spec() -> BoardProviderSpec:
    """Registry metadata Linear: PAT в env, ключ команды — non-secret option."""
    return BoardProviderSpec(
        board_type="linear",
        factory=_build_provider,
        credential_fields=(
            CredentialFieldSpec("LINEAR_API_KEY", "Linear personal API key", secret=True),
            CredentialFieldSpec(
                "LINEAR_API_BASE",
                "Linear GraphQL API base URL",
                secret=False,
                required=False,
                default=DEFAULT_API_BASE,
            ),
        ),
        option_fields=(
            ProviderOptionSpec(
                "team_key",
                "Team key",
                required_for=("sync", "create"),
            ),
        ),
        setup=ProviderSetupSpec(
            "Linear",
            "https://linear.app/settings/account/security",
            (
                "Settings → Account → Security & access → Personal API keys: создайте "
                "ключ с правами Read и Write (плюс Create issues, если нужен create-task) "
                "и, если ограничиваете доступ, разрешите нужные команды. Ключ виден "
                "только при создании; OAuth не поддерживается."
            ),
        ),
        default_api_base=DEFAULT_API_BASE,
        create_target_label="Workflow state создания",
        done_target_label="Workflow state завершения",
    )


def _timestamp(value: object) -> int:
    """ISO 8601 ``updatedAt`` → epoch ms; naive-время трактуется как UTC."""
    if not isinstance(value, str) or not value:
        return 0
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _nodes(block: object) -> list[dict]:
    """Узлы GraphQL-connection; отсутствующий блок → пустой список."""
    if not isinstance(block, Mapping):
        return []
    return [node for node in (block.get("nodes") or []) if isinstance(node, Mapping)]


def _subtasks(issue: Mapping[str, Any]) -> list[dict]:
    """Sub-issues задачи: ``children`` из схемы Linear."""
    return [
        {"key": str(node.get("identifier") or ""), "title": str(node.get("title") or "")}
        for node in _nodes(issue.get("children"))
        if node.get("identifier")
    ]


def _attachments(issue: Mapping[str, Any]) -> list[dict]:
    """Метаданные вложений: у Linear есть только ``title``/``url`` (без mime и size)."""
    return [
        {
            "name": str(node.get("title") or ""),
            "url": str(node.get("url") or ""),
            "mime": None,
            "size": None,
        }
        for node in _nodes(issue.get("attachments"))
        if node.get("url")
    ]


class LinearBoard(RestBoardBase):
    """Адаптер Linear поверх общего GraphQL-фасада."""

    board_type = "linear"

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str = DEFAULT_API_BASE,
        team_key: str = "",
        key_pattern: str = "",
        url_template: str = "",
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        attempts: int = 3,
    ) -> None:
        super().__init__(
            base_url=(api_base or DEFAULT_API_BASE).rstrip("/"),
            secrets=(api_key,),
            key_pattern=key_pattern,
            url_template=url_template,
            # Personal API key идёт БЕЗ префикса Bearer — это требование Linear.
            headers={"Authorization": api_key, "Accept": "application/json"},
            transport=transport,
            sleeper=sleeper,
            attempts=attempts,
            rate_limit_hint=_rate_limit_hint,
        )
        self._team_key = (team_key or "").strip()
        self._gql = GraphQLClient(self._http, path="/graphql", secrets=self.secrets)

    @staticmethod
    def _raw_from_issue(issue: Mapping[str, Any]) -> RawTask:
        """Единый маппер узла issue → RawTask (общий для listing и fetch_one)."""
        identifier = str(issue.get("identifier") or "")
        state = issue.get("state") if isinstance(issue.get("state"), Mapping) else {}
        team = issue.get("team") if isinstance(issue.get("team"), Mapping) else {}
        subtasks = _subtasks(issue)
        return RawTask(
            key=identifier,
            project_code=identifier,
            title=str(issue.get("title") or ""),
            description=str(issue.get("description") or ""),
            status=state.get("name"),
            subtask_ids=[item["key"] for item in subtasks],
            timestamp=_timestamp(issue.get("updatedAt")),
            links=[],
            attachments=_attachments(issue),
            board_id=str(issue.get("id") or ""),
            provider_data={
                "subtasks": subtasks,
                "state": {
                    "id": state.get("id"),
                    "name": state.get("name"),
                    "type": state.get("type"),
                },
                "team": {"id": team.get("id"), "key": team.get("key")},
                "url": str(issue.get("url") or ""),
            },
        )

    @staticmethod
    def _issue_filter(board: str | None) -> dict | None:
        """Фильтр листинга: скоуп по ключу команды; без команды — весь workspace.

        Серверный ``updatedAt: {gt: …}`` здесь намеренно не применяется: SyncService
        строит по полному перечислению список активных ключей (purge), а срез по
        watermark он делает сам.
        """
        key = (board or "").strip()
        return {"team": {"key": {"eq": key}}} if key else None

    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        """Все задачи команды: курсорная пагинация ``issues`` по ``pageInfo``."""
        count = 0
        nodes = self._gql.paginate(
            _ISSUES_QUERY,
            {"filter": self._issue_filter(board or self._team_key), "first": _PAGE},
            connection=lambda data: data.get("issues") or {},
        )
        for node in nodes:
            yield self._raw_from_issue(node)
            count += 1
            if limit is not None and count >= limit:
                return

    def fetch_one(self, key: str) -> RawTask | None:
        """Одна задача по ключу: ``issue(id:)`` принимает и UUID, и ``ENG-123``.

        fail-soft: нет задачи (``ENTITY_NOT_FOUND``/``null``) → None; остальные
        категории ошибок пробрасываются как есть.
        """
        try:
            data = self._gql.execute(_ISSUE_QUERY, {"id": key})
        except BoardProviderError as error:
            if error.category == "not_found":
                return None
            raise
        issue = data.get("issue")
        return self._raw_from_issue(issue) if isinstance(issue, Mapping) else None

    def _teams(self, key: str | None) -> list[dict]:
        """Команды workspace: с фильтром по ключу (``TeamFilter.key``) либо все."""
        data = self._gql.execute(
            _TEAMS_QUERY,
            {"filter": {"key": {"eq": key}} if key else None, "first": _TEAMS_PAGE},
        )
        return _nodes(data.get("teams"))

    def _resolve_team(self, project: str | None) -> tuple[dict | None, list[dict], list[str]]:
        """Команда по ключу (``ENG``); без ключа годится единственная команда workspace."""
        key = (project or self._team_key).strip()
        teams = self._teams(key or None)
        if key:
            exact = [team for team in teams if str(team.get("key") or "") == key]
            if exact:
                return exact[0], teams, []
            return None, teams, [f"Linear team {key!r} is unavailable."]
        if len(teams) == 1:
            return teams[0], teams, []
        return None, teams, ["Linear team key is required: workspace has several teams."]

    @staticmethod
    def _states(team: Mapping[str, Any] | None) -> list[dict]:
        """Workflow states команды в порядке доски (``position``)."""
        states = _nodes((team or {}).get("states"))
        return sorted(states, key=lambda state: (state.get("position") or 0.0, state.get("name")))

    @staticmethod
    def _resolve_state(
        states: list[dict],
        target: str | None,
    ) -> tuple[dict | None, list[str]]:
        """Workflow state по id или точному имени; без target — первый ``completed``.

        Тип состояния (``completed``) не зависит от языка workspace, поэтому
        done-цель по умолчанию угадывается безопасно — в отличие от досок с
        локализованными именами статусов.
        """
        if target:
            by_id = [state for state in states if str(state.get("id") or "") == target]
            if by_id:
                return by_id[0], []
            by_name = [state for state in states if str(state.get("name") or "") == target]
            if len(by_name) == 1:
                return by_name[0], []
            if len(by_name) > 1:
                return None, [f"Linear target {target!r} is ambiguous; state was not applied."]
            return None, [f"Linear target {target!r} is unavailable; state was not applied."]
        completed = [
            state
            for state in states
            if str(state.get("type") or "") == "completed"
        ]
        if completed:
            return completed[0], []
        return None, ["Linear team has no completed workflow state; state was not applied."]

    def validate_connection(self, project: str | None = None) -> dict:
        """Проверить токен (``viewer``) и доступность команды проекта."""
        data = self._gql.execute(_VIEWER_QUERY)
        viewer = data.get("viewer") if isinstance(data.get("viewer"), Mapping) else {}
        organization = (
            data.get("organization") if isinstance(data.get("organization"), Mapping) else {}
        )
        capabilities = {"read": True, "create": False, "finish": False}
        team, _teams_list, warnings = self._resolve_team(project)
        if team is not None:
            capabilities["create"] = bool(team.get("id"))
            capabilities["finish"] = any(
                str(state.get("type") or "") in _DONE_STATE_TYPES
                for state in self._states(team)
            )
            if not capabilities["finish"]:
                warnings.append(
                    "Linear team has no terminal workflow state; the done target is unknown."
                )
        return {
            "status": "ok",
            "identity": {
                "user_id": str(viewer.get("id") or ""),
                "display_name": str(viewer.get("displayName") or viewer.get("name") or ""),
                "organization": str(organization.get("urlKey") or organization.get("name") or ""),
            },
            "project": project,
            "capabilities": capabilities,
            "warnings": warnings,
        }

    def list_targets(self, project: str | None) -> dict:
        """Workflow states команды в общей discovery-модели targets/options."""
        team, teams, warnings = self._resolve_team(project)
        targets = [
            {
                "id": str(state.get("id") or ""),
                "label": str(state.get("name") or state.get("id") or ""),
                "purposes": ["create", "done"]
                if str(state.get("type") or "") in _DONE_STATE_TYPES
                else ["create"],
                "type": str(state.get("type") or ""),
            }
            for state in self._states(team)
            if state.get("id")
        ]
        return {
            "targets": targets,
            "options": [
                {
                    "key": "team_key",
                    "label": "Team key",
                    "required_for": ["sync", "create"],
                    "choices": [
                        {
                            "id": str(item.get("key") or ""),
                            "label": str(item.get("name") or item.get("key") or ""),
                        }
                        for item in teams
                        if item.get("key")
                    ],
                }
            ],
            "warnings": warnings,
        }

    def _update_issue(self, issue_id: str, payload: dict) -> tuple[bool, list[str]]:
        """Одно ``issueUpdate``; fail-soft: ошибка → (False, warning)."""
        try:
            data = self._gql.execute(
                _ISSUE_UPDATE_MUTATION,
                {"id": issue_id, "input": payload},
                operation="write",
            )
        except BoardProviderError as error:
            return False, [f"Linear issueUpdate failed: {error.category}."]
        result = data.get("issueUpdate") if isinstance(data.get("issueUpdate"), Mapping) else {}
        if not result.get("success"):
            return False, ["Linear issueUpdate did not report success."]
        return True, []

    def finish(
        self,
        key: str,
        pr_url: str,
        *,
        note: str | None = None,
        mark_done: bool = True,
        target: str | None = None,
    ) -> dict:
        """Дописать PR-ссылку в markdown-описание и перевести задачу в done-state.

        Обе правки — независимые ``issueUpdate``: сбой перевода состояния не
        отменяет уже записанную ссылку. Идемпотентность: ссылка ищется в описании
        по самому URL, состояние сравнивается с целевым до записи.
        """
        data = self._gql.execute(_ISSUE_FINISH_QUERY, {"id": key})
        issue = data.get("issue") if isinstance(data.get("issue"), Mapping) else None
        if not issue:
            raise BoardProviderError(
                "not_found",
                "Linear issue was not found.",
                hint="Check the issue identifier and the API key team access.",
                secrets=self.secrets,
            )
        issue_id = str(issue.get("id") or key)
        description = str(issue.get("description") or "")
        warnings: list[str] = []

        link_present = bool(pr_url) and pr_url in description
        pr_link_added = False
        if pr_url and not link_present:
            block = f"\n\nPR: {pr_url}" + (f"\n\n{note}" if note else "")
            updated = description + block if description else block.lstrip("\n")
            pr_link_added, link_warnings = self._update_issue(
                issue_id,
                {"description": updated},
            )
            warnings.extend(link_warnings)

        current = issue.get("state") if isinstance(issue.get("state"), Mapping) else {}
        states = self._states(issue.get("team"))
        done_set = False
        already_at_target = False
        if mark_done:
            resolved, state_warnings = self._resolve_state(states, target)
            warnings.extend(state_warnings)
            if resolved is not None:
                if str(current.get("id") or "") == str(resolved.get("id") or ""):
                    already_at_target = True
                else:
                    done_set, update_warnings = self._update_issue(
                        issue_id,
                        {"stateId": str(resolved.get("id") or "")},
                    )
                    warnings.extend(update_warnings)

        link_satisfied = not pr_url or link_present or pr_link_added
        done_satisfied = not mark_done or already_at_target or done_set
        return {
            "key": key,
            "board_id": issue_id,
            "done_set": done_set,
            "pr_link_added": pr_link_added,
            "already_closed": (
                not pr_link_added and not done_set and link_satisfied and done_satisfied
            ),
            "warnings": warnings,
        }

    def create(
        self,
        doc_md: str,
        *,
        title: str,
        target: str | None,
        project: str | None,
    ) -> dict:
        """Создать issue: ``issueCreate`` требует ``teamId``, описание — markdown как есть."""
        team, _teams_list, team_warnings = self._resolve_team(project)
        if team is None or not team.get("id"):
            raise BoardProviderError(
                "configuration",
                "Linear team is required to create an issue.",
                hint="Set the team_key provider option or pass the team key as project.",
                secrets=self.secrets,
            )
        warnings: list[str] = list(team_warnings)
        state: dict | None = None
        if target:
            state, state_warnings = self._resolve_state(self._states(team), target)
            warnings.extend(state_warnings)
        payload: dict[str, Any] = {
            "teamId": str(team["id"]),
            "title": title,
            "description": doc_md,
        }
        if state is not None:
            payload["stateId"] = str(state.get("id") or "")
        data = self._gql.execute(
            _ISSUE_CREATE_MUTATION,
            {"input": payload},
            operation="write",
        )
        result = data.get("issueCreate") if isinstance(data.get("issueCreate"), Mapping) else {}
        issue = result.get("issue") if isinstance(result.get("issue"), Mapping) else {}
        key = str(issue.get("identifier") or "")
        if not result.get("success") or not key:
            raise BoardProviderError(
                "unsupported",
                "Linear issueCreate did not return a created issue.",
                hint="Check the Linear team permissions and required issue fields.",
                secrets=self.secrets,
            )
        applied = issue.get("state") if isinstance(issue.get("state"), Mapping) else {}
        target_resolved = (
            str(applied.get("id") or applied.get("name") or "")
            or (str(state.get("id") or "") if state else "")
            or None
        )
        return {
            "key": key,
            "url": str(issue.get("url") or "") or self._task_url(key),
            "board_id": str(issue.get("id") or key),
            "target_resolved": target_resolved,
            "warnings": warnings,
        }

    def _brief(self, raw: RawTask, *, with_details: bool) -> dict:
        subtasks = raw.provider_data.get("subtasks") or []
        links = [
            {"type": "subtask", "key": item["key"], "title": item.get("title") or ""}
            for item in subtasks
        ]
        links.extend(raw.links)
        covered = {raw.key, *(str(item.get("key") or "") for item in links)}
        if self._key_pattern:
            for match in re.finditer(self._key_pattern, raw.description or ""):
                code = match.group(0)
                if code not in covered:
                    links.append({"type": "related", "key": code, "title": ""})
                    covered.add(code)

        attachments: list[dict] = []
        warnings: list[str] = []
        if with_details and raw.attachments:
            attachments = [
                {
                    "name": item.get("name") or "",
                    "mime_type": item.get("mime"),
                    "size": item.get("size"),
                    # Файлы Linear лежат в приватном облаке и требуют авторизации
                    # даже на скачивание — сохраняем только метаданные.
                    "content_text": None,
                    "url": item.get("url") or "",
                }
                for item in raw.attachments
            ]
            warnings.append(
                "вложения Linear сохранены метаданными: содержимое приватного "
                "облака требует отдельной авторизации"
            )
        return {
            "key": raw.key,
            "aliases": [raw.board_id] if raw.board_id else [],
            "title": raw.title,
            "description": raw.description,
            "criteria": [item.get("title") or item["key"] for item in subtasks]
            if with_details
            else [],
            "status": raw.status,
            "url": raw.provider_data.get("url") or self._task_url(raw.key) or None,
            "links": links,
            "project": project_prefix(raw.key),
            "attachments": attachments,
            "warnings": warnings,
        }

    def normalize(self, raw: RawTask) -> dict:
        return self._brief(raw, with_details=True)

    def normalize_meta(self, raw: RawTask) -> dict:
        return self._brief(raw, with_details=False)
