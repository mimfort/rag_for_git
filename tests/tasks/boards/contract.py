"""Повторно используемый contract-test набор для адаптеров досок."""
from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from reviewer.tasks.boards.base import TaskBoardProvider
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.jira import JiraCloudBoard
from reviewer.tasks.boards.yougile import YougileBoard
from reviewer.tasks.boards.youtrack import YouTrackBoard


@dataclass
class _State:
    calls: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    closed: bool = False
    task_description: str = "<p>Описание PRI-2</p>"
    task_completed: bool = False
    task_column: str = "open-id"
    issue_description: str = "Описание PRI-2"
    issue_state: str = "Open"
    jira_description: dict = field(
        default_factory=lambda: {"type": "doc", "version": 1, "content": []}
    )
    jira_state_id: str = "1"
    created: int = 0


class _RecordingTransport(httpx.MockTransport):
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response], state: _State):
        super().__init__(handler)
        self._state = state

    def close(self) -> None:
        self._state.closed = True
        super().close()


@dataclass(frozen=True)
class ProviderAdapter:
    board_type: str
    secret: str
    project: str
    key: str
    target_id: str
    target_label: str
    missing_target: str

    def provider_factory(
        self,
        *,
        state: _State | None = None,
        forbidden: bool = False,
        error_status: int | None = None,
    ) -> tuple[TaskBoardProvider, _State]:
        state = state or _State()
        status = error_status or (403 if forbidden else None)
        handler = {
            "yougile": _yougile_handler,
            "youtrack": _youtrack_handler,
            "jira": _jira_handler,
        }[self.board_type](state, error_status=status)
        if self.board_type == "jira":
            return (
                JiraCloudBoard(
                    base_url="https://jira-contract.atlassian.net",
                    email="robot@example.test",
                    api_token=self.secret,
                    key_pattern=r"PRI-\d+",
                    issue_type="10001",
                    attachment_max_bytes=1000,
                    attachment_timeout=1.0,
                    attachment_store_chars=1000,
                    transport=_RecordingTransport(handler, state),
                ),
                state,
            )
        raw_client = httpx.Client(
            base_url=(
                "https://yougile.test/api-v2"
                if self.board_type == "yougile"
                else "https://youtrack.test/api"
            ),
            transport=_RecordingTransport(handler, state),
        )
        if self.board_type == "yougile":
            provider = YougileBoard(
                api_key=self.secret,
                api_base="https://yougile.test/api-v2",
                key_pattern=r"PRI-\d+",
                url_template="https://yougile.test/#task/{code}",
            )
        else:
            provider = YouTrackBoard(
                token=self.secret,
                base_url="https://youtrack.test/api",
                key_pattern=r"PRI-\d+",
                status_field="State",
            )
        provider._client.close()  # type: ignore[attr-defined]
        provider._client = raw_client  # type: ignore[attr-defined]
        return provider, state


ADAPTERS = (
    ProviderAdapter(
        board_type="yougile",
        secret="yougile-contract-secret",
        project="PRI",
        key="ID-1",
        target_id="done-id",
        target_label="Done",
        missing_target="Missing",
    ),
    ProviderAdapter(
        board_type="youtrack",
        secret="perm:youtrack-contract-secret",
        project="PRI",
        key="PRI-1",
        target_id="Done",
        target_label="Done",
        missing_target="Missing",
    ),
    ProviderAdapter(
        board_type="jira",
        secret="jira-contract-secret",
        project="PRI",
        key="PRI-1",
        target_id="2",
        target_label="Done",
        missing_target="Missing",
    ),
)


def _json(request: httpx.Request) -> dict[str, Any]:
    import json

    return json.loads(request.content.decode()) if request.content else {}


def _record(state: _State, request: httpx.Request) -> None:
    state.calls.append(
        (request.method, request.url.path, dict(request.url.params.multi_items()))
    )


def _yougile_task(
    number: int,
    *,
    description: str | None = None,
    completed: bool = False,
    column_id: str = "open-id",
) -> dict[str, Any]:
    return {
        "id": f"uuid-{number}",
        "idTaskCommon": f"ID-{number}",
        "idTaskProject": f"PRI-{number}",
        "title": f"Задача {number}",
        "description": description if description is not None else f"<p>Описание PRI-{number}</p>",
        "columnId": column_id,
        "subtasks": ["sub-1"] if number == 1 else [],
        "timestamp": 1000 + number,
        "completed": completed,
    }


def _yougile_handler(
    state: _State,
    *,
    error_status: int | None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        _record(state, request)
        path = request.url.path.removeprefix("/api-v2")
        params = request.url.params
        if error_status is not None:
            return httpx.Response(error_status, json={"token": "must-not-leak"})
        if request.method == "GET" and path == "/companies":
            return httpx.Response(200, json={"content": [{"id": "company-1", "name": "Acme"}]})
        if request.method == "GET" and path == "/projects":
            return httpx.Response(200, json={"content": [{"id": "project-1", "title": "PRI"}]})
        if request.method == "GET" and path == "/boards":
            return httpx.Response(200, json={"content": [{"id": "board-1", "title": "Main"}]})
        if request.method == "GET" and path == "/columns":
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"id": "open-id", "title": "Open"},
                        {"id": "done-id", "title": "Done"},
                    ]
                },
            )
        if request.method == "GET" and path == "/tasks":
            column = params.get("columnId")
            offset = int(params.get("offset", "0"))
            if column == "open-id":
                tasks = [_yougile_task(n) for n in range(1, 1002)]
                return httpx.Response(200, json={"content": tasks[offset : offset + 1000]})
            return httpx.Response(200, json={"content": []})
        if request.method == "GET" and path == "/tasks/sub-1":
            return httpx.Response(
                200,
                json={"id": "sub-1", "idTaskCommon": "ID-9", "title": "Подзадача"},
            )
        if request.method == "GET" and path == "/tasks/ID-1":
            return httpx.Response(200, json=_yougile_task(1))
        if request.method == "GET" and path == "/tasks/ID-2":
            return httpx.Response(
                200,
                json=_yougile_task(
                    2,
                    description=state.task_description,
                    completed=state.task_completed,
                    column_id=state.task_column,
                ),
            )
        if request.method == "GET" and path == "/tasks/uuid-created":
            return httpx.Response(200, json={"id": "uuid-created", "idTaskProject": "PRI-77"})
        if request.method == "GET" and path == "/columns/open-id":
            return httpx.Response(200, json={"id": "open-id", "title": "Open", "boardId": "board-1"})
        if request.method == "GET" and path == "/columns/done-id":
            return httpx.Response(200, json={"id": "done-id", "title": "Done", "boardId": "board-1"})
        if request.method == "GET" and path == "/chats/uuid-1/messages":
            return httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "text": "/root/#file:https://yougile.test/user-data/spec.txt",
                            "properties": {},
                        }
                    ]
                },
            )
        if request.method == "GET" and path == "/user-data/spec.txt":
            return httpx.Response(200, text="Критерий из вложения")
        if request.method == "POST" and path == "/tasks":
            state.created += 1
            return httpx.Response(200, json={"id": "uuid-created"})
        if request.method == "PUT" and path == "/tasks/uuid-2":
            payload = _json(request)
            state.task_description = payload.get("description", state.task_description)
            state.task_completed = payload.get("completed", state.task_completed)
            state.task_column = payload.get("columnId", state.task_column)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    return handle


def _youtrack_issue(
    number: int,
    *,
    description: str | None = None,
    state_name: str = "Open",
) -> dict[str, Any]:
    return {
        "idReadable": f"PRI-{number}",
        "summary": f"Задача {number}",
        "description": description if description is not None else f"Описание PRI-{number}",
        "updated": 2000 + number,
        "customFields": [
            {
                "name": "State",
                "$type": "StateIssueCustomField",
                "value": {"name": state_name, "$type": "StateBundleElement"},
            }
        ],
        "links": [
            {
                "linkType": {"name": "Subtask"},
                "issues": [{"idReadable": "PRI-9"}],
            }
        ]
        if number == 1
        else [],
        "attachments": [
            {
                "name": "spec.txt",
                "mimeType": "text/plain",
                "size": 24,
                "url": "/files/spec.txt",
            }
        ]
        if number == 1
        else [],
    }


def _youtrack_handler(
    state: _State,
    *,
    error_status: int | None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        _record(state, request)
        path = request.url.path.removeprefix("/api")
        params = request.url.params
        if error_status is not None:
            return httpx.Response(error_status, json={"token": "must-not-leak"})
        if request.method == "GET" and path == "/users/me":
            return httpx.Response(200, json={"id": "user-1", "login": "robot", "name": "Robot"})
        if request.method == "GET" and path == "/admin/projects":
            return httpx.Response(200, json=[{"id": "project-1", "shortName": "PRI", "name": "PRI"}])
        if request.method == "GET" and path == "/admin/projects/project-1/customFields":
            return httpx.Response(
                200,
                json=[
                    {
                        "$type": "StateProjectCustomField",
                        "field": {"name": "State"},
                        "bundle": {
                            "values": [
                                {"name": "Open", "$type": "StateBundleElement"},
                                {"name": "Done", "$type": "StateBundleElement"},
                            ]
                        },
                    }
                ],
            )
        if request.method == "GET" and path == "/issues" and "$skip" in params:
            skip = int(params.get("$skip", "0"))
            issues = [_youtrack_issue(n) for n in range(1, 202)]
            return httpx.Response(200, json=issues[skip : skip + 200])
        if request.method == "GET" and path == "/issues":
            return httpx.Response(200, json=[_youtrack_issue(1)])
        if request.method == "GET" and path == "/issues/PRI-1":
            return httpx.Response(200, json=_youtrack_issue(1))
        if request.method == "GET" and path == "/issues/PRI-2":
            return httpx.Response(
                200,
                json=_youtrack_issue(
                    2,
                    description=state.issue_description,
                    state_name=state.issue_state,
                ),
            )
        if request.method == "GET" and path == "/issues/PRI-77":
            return httpx.Response(200, json=_youtrack_issue(77))
        if request.method == "GET" and path == "/files/spec.txt":
            return httpx.Response(200, text="Критерий из вложения")
        if request.method == "POST" and path == "/issues" and "fields" in params:
            state.created += 1
            return httpx.Response(200, json={"idReadable": "PRI-77"})
        if request.method == "POST" and path in {"/issues/PRI-2", "/issues/PRI-77"}:
            payload = _json(request)
            if "description" in payload:
                state.issue_description = payload["description"]
            for custom_field in payload.get("customFields", []):
                state.issue_state = (custom_field.get("value") or {}).get("name", state.issue_state)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    return handle


def _jira_issue(number: int) -> dict[str, Any]:
    return {
        "id": str(10000 + number),
        "key": f"PRI-{number}",
        "fields": {
            "summary": f"Задача {number}",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": f"Описание PRI-{number}"}]}
                ],
            },
            "status": {"id": "1", "name": "Open"},
            "updated": f"2026-07-23T09:{number % 60:02d}:00.000Z",
            "subtasks": [{"key": "PRI-9", "fields": {"summary": "Подзадача"}}] if number == 1 else [],
            "issuelinks": [],
            "attachment": [
                {
                    "filename": "spec.txt",
                    "mimeType": "text/plain",
                    "size": 24,
                    "content": "https://jira-contract.atlassian.net/files/spec.txt",
                }
            ] if number == 1 else [],
            "issuetype": {"id": "10001", "name": "Task"},
            "project": {"id": "10000", "key": "PRI", "name": "PRI"},
        },
    }


def _jira_state_issue(state: _State, number: int) -> dict[str, Any]:
    result = _jira_issue(number)
    result["fields"]["description"] = state.jira_description
    result["fields"]["status"] = {
        "id": state.jira_state_id,
        "name": state.issue_state,
    }
    return result


def _jira_handler(
    state: _State,
    *,
    error_status: int | None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        _record(state, request)
        if error_status is not None:
            return httpx.Response(error_status, json={"token": "must-not-leak"})
        path = request.url.path
        if request.method == "GET" and path == "/rest/api/3/myself":
            return httpx.Response(
                200,
                json={"accountId": "user-1", "displayName": "Robot"},
            )
        if request.method == "GET" and path == "/rest/api/3/project/PRI/statuses":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "10001",
                        "name": "Task",
                        "statuses": [
                            {"id": "1", "name": "Open"},
                            {"id": "2", "name": "Done"},
                        ],
                    }
                ],
            )
        if request.method == "GET" and path == "/rest/api/3/project/PRI":
            return httpx.Response(200, json={"id": "10000", "key": "PRI"})
        if request.method == "GET" and path == "/rest/api/3/mypermissions":
            return httpx.Response(
                200,
                json={
                    "permissions": {
                        name: {"havePermission": True}
                        for name in (
                            "BROWSE_PROJECTS",
                            "CREATE_ISSUES",
                            "TRANSITION_ISSUES",
                        )
                    }
                },
            )
        if request.method == "POST" and request.url.path == "/rest/api/3/search/jql":
            payload = _json(request)
            start = 200 if payload.get("nextPageToken") else 0
            issues = [_jira_issue(number) for number in range(1, 202)]
            page = {"issues": issues[start : start + 200]}
            if start == 0:
                page["nextPageToken"] = "next"
            else:
                page["isLast"] = True
            return httpx.Response(200, json=page)
        if request.method == "GET" and path == "/rest/api/3/issue/PRI-1":
            return httpx.Response(200, json=_jira_issue(1))
        if request.method == "GET" and path in {
            "/rest/api/3/issue/PRI-2",
            "/rest/api/3/issue/PRI-77",
        }:
            number = 2 if path.endswith("PRI-2") else 77
            return httpx.Response(200, json=_jira_state_issue(state, number))
        if request.method == "GET" and path.endswith("/transitions"):
            return httpx.Response(
                200,
                json={
                    "transitions": [
                        {"id": "21", "name": "Complete", "to": {"id": "2", "name": "Done"}}
                    ]
                },
            )
        if request.method == "GET" and path == "/files/spec.txt":
            return httpx.Response(200, text="Критерий из вложения")
        if request.method == "POST" and path == "/rest/api/3/issue":
            state.created += 1
            state.jira_description = (_json(request).get("fields") or {}).get(
                "description", state.jira_description
            )
            return httpx.Response(201, json={"id": "10077", "key": "PRI-77"})
        if request.method == "PUT" and path in {
            "/rest/api/3/issue/PRI-2",
            "/rest/api/3/issue/PRI-77",
        }:
            state.jira_description = (_json(request).get("fields") or {}).get(
                "description", state.jira_description
            )
            return httpx.Response(204)
        if request.method == "POST" and path.endswith("/transitions"):
            state.jira_state_id = "2"
            state.issue_state = "Done"
            return httpx.Response(204)
        return httpx.Response(404, json={})

    return handle


class ProviderContract:
    """Общие поведенческие проверки полного provider contract."""

    @pytest.fixture
    def adapter(self, request: pytest.FixtureRequest) -> ProviderAdapter:
        return request.param

    def test_validate_connection_is_safe(self, adapter: ProviderAdapter) -> None:
        provider, _ = adapter.provider_factory()
        result = provider.validate_connection(adapter.project)
        assert set(result) == {"status", "identity", "project", "capabilities", "warnings"}
        assert result["status"] == "ok"
        assert adapter.secret not in repr(result)

    def test_iter_raw_reads_all_pages_and_maps_stable_timestamp(
        self,
        adapter: ProviderAdapter,
    ) -> None:
        provider, state = adapter.provider_factory()
        rows = list(provider.iter_raw(adapter.project, None))
        assert len(rows) > (1000 if adapter.board_type == "yougile" else 200)
        assert rows[0].timestamp > 0
        page_calls = [
            call
            for call in state.calls
            if call[1].endswith(("/tasks", "/issues", "/search/jql"))
        ]
        assert len(page_calls) >= 2

    def test_normalize_meta_has_zero_http_budget(self, adapter: ProviderAdapter) -> None:
        provider, state = adapter.provider_factory()
        raw = next(iter(provider.iter_raw(adapter.project, 1)))
        state.calls.clear()
        result = provider.normalize_meta(raw)
        assert state.calls == []
        assert result["key"] == raw.key
        assert result["attachments"] == []

    def test_normalize_preserves_markdown_links_subtasks_and_attachments(
        self,
        adapter: ProviderAdapter,
    ) -> None:
        provider, _ = adapter.provider_factory()
        raw = next(iter(provider.iter_raw(adapter.project, 1)))
        result = provider.normalize(raw)
        assert "<p>" not in result["description"]
        assert any(link["type"] == "subtask" for link in result["links"])
        assert result["attachments"][0]["name"] == "spec.txt"

    def test_fetch_one_matches_iter_mapper(self, adapter: ProviderAdapter) -> None:
        provider, _ = adapter.provider_factory()
        raw = next(iter(provider.iter_raw(adapter.project, 1)))
        one = provider.fetch_one(raw.key)
        assert one is not None
        assert dataclasses.asdict(one) == dataclasses.asdict(raw)

    def test_create_reports_exact_target(self, adapter: ProviderAdapter) -> None:
        provider, _ = adapter.provider_factory()
        result = provider.create(
            "# Новая задача",
            title="Новая задача",
            target=adapter.target_id,
            project=adapter.project,
        )
        assert result["key"]
        assert result["target_resolved"] in {adapter.target_id, adapter.target_label}
        assert result["warnings"] == []

    def test_create_falls_back_with_warning(self, adapter: ProviderAdapter) -> None:
        provider, _ = adapter.provider_factory()
        result = provider.create(
            "# Новая задача",
            title="Новая задача",
            target=adapter.missing_target,
            project=adapter.project,
        )
        assert result["key"]
        assert result["target_resolved"] != adapter.missing_target
        assert result["warnings"]

    def test_list_targets_uses_normalized_shape(self, adapter: ProviderAdapter) -> None:
        provider, _ = adapter.provider_factory()
        result = provider.list_targets(adapter.project)
        assert set(result) == {"targets", "options", "warnings"}
        assert {"id", "label", "purposes"} <= set(result["targets"][0])
        assert all("create" in target["purposes"] for target in result["targets"])

    def test_finish_is_idempotent_and_uses_target(self, adapter: ProviderAdapter) -> None:
        provider, _ = adapter.provider_factory()
        first = provider.finish(
            adapter.key.replace("1", "2"),
            "https://github.test/pull/7",
            note="Проверено",
            target=adapter.target_id,
        )
        second = provider.finish(
            adapter.key.replace("1", "2"),
            "https://github.test/pull/7",
            note="Проверено",
            target=adapter.target_id,
        )
        assert first["pr_link_added"] is True
        assert first["done_set"] is True
        assert second["pr_link_added"] is False
        assert second["done_set"] is False
        assert second["already_closed"] is True

    def test_close_closes_transport_after_success(self, adapter: ProviderAdapter) -> None:
        provider, state = adapter.provider_factory()
        provider.validate_connection(adapter.project)
        provider.close()
        assert state.closed is True

    def test_close_closes_transport_after_error(self, adapter: ProviderAdapter) -> None:
        provider, state = adapter.provider_factory(forbidden=True)
        with pytest.raises(BoardProviderError) as exc_info:
            provider.validate_connection(adapter.project)
        assert exc_info.value.category == "permission"
        provider.close()
        assert state.closed is True

    def test_not_found_transport_error_is_mapped(self, adapter: ProviderAdapter) -> None:
        provider, _ = adapter.provider_factory(error_status=404)
        with pytest.raises(BoardProviderError) as exc_info:
            provider.validate_connection(adapter.project)
        assert exc_info.value.category == "not_found"

    def test_secrets_absent_from_error_result_and_logs(
        self,
        adapter: ProviderAdapter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        provider, _ = adapter.provider_factory(forbidden=True)
        with caplog.at_level(logging.WARNING), pytest.raises(BoardProviderError) as exc_info:
            provider.validate_connection(adapter.project)
        text = f"{exc_info.value!s}\n{exc_info.value!r}\n{caplog.text}"
        assert adapter.secret not in text
