"""Фейк доски Jira Cloud для общего contract-набора."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from reviewer.tasks.boards.jira import JiraCloudBoard
from tests.tasks.boards.fakes.base import (
    FakeState,
    ProviderAdapter,
    RecordingTransport,
    record,
    request_json,
)

SECRET = "jira-contract-secret"
BASE_URL = "https://jira-contract.atlassian.net"


@dataclass
class State(FakeState):
    """Состояние фейка Jira: описание/статус задачи и счётчик созданий."""

    jira_description: dict = field(
        default_factory=lambda: {"type": "doc", "version": 1, "content": []}
    )
    jira_state_id: str = "1"
    issue_state: str = "Open"
    created: int = 0


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
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": f"Описание PRI-{number}"}],
                    }
                ],
            },
            "status": {"id": "1", "name": "Open"},
            "updated": f"2026-07-23T09:{number % 60:02d}:00.000Z",
            "subtasks": [{"key": "PRI-9", "fields": {"summary": "Подзадача"}}]
            if number == 1
            else [],
            "issuelinks": [],
            "attachment": [
                {
                    "filename": "spec.txt",
                    "mimeType": "text/plain",
                    "size": 24,
                    "content": f"{BASE_URL}/files/spec.txt",
                }
            ]
            if number == 1
            else [],
            "issuetype": {"id": "10001", "name": "Task"},
            "project": {"id": "10000", "key": "PRI", "name": "PRI"},
        },
    }


def _jira_state_issue(state: State, number: int) -> dict[str, Any]:
    result = _jira_issue(number)
    result["fields"]["description"] = state.jira_description
    result["fields"]["status"] = {
        "id": state.jira_state_id,
        "name": state.issue_state,
    }
    return result


def _jira_handler(
    state: State,
    *,
    error_status: int | None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        record(state, request)
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
        if request.method == "POST" and path == "/rest/api/3/search/jql":
            payload = request_json(request)
            start = 200 if payload.get("nextPageToken") else 0
            issues = [_jira_issue(number) for number in range(1, 202)]
            page: dict[str, Any] = {"issues": issues[start : start + 200]}
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
            state.jira_description = (request_json(request).get("fields") or {}).get(
                "description", state.jira_description
            )
            return httpx.Response(201, json={"id": "10077", "key": "PRI-77"})
        if request.method == "PUT" and path in {
            "/rest/api/3/issue/PRI-2",
            "/rest/api/3/issue/PRI-77",
        }:
            state.jira_description = (request_json(request).get("fields") or {}).get(
                "description", state.jira_description
            )
            return httpx.Response(204)
        if request.method == "POST" and path.endswith("/transitions"):
            state.jira_state_id = "2"
            state.issue_state = "Done"
            return httpx.Response(204)
        return httpx.Response(404, json={})

    return handle


def build(
    *,
    state: State | None = None,
    forbidden: bool = False,
    error_status: int | None = None,
) -> tuple[JiraCloudBoard, State]:
    """Собрать JiraCloudBoard с транспортом через конструктор (без сети)."""
    state = state or State()
    status = error_status or (403 if forbidden else None)
    provider = JiraCloudBoard(
        base_url=BASE_URL,
        email="robot@example.test",
        api_token=SECRET,
        key_pattern=r"PRI-\d+",
        issue_type="10001",
        attachment_max_bytes=1000,
        attachment_timeout=1.0,
        attachment_store_chars=1000,
        transport=RecordingTransport(_jira_handler(state, error_status=status), state),
    )
    return provider, state


ADAPTER = ProviderAdapter(
    board_type="jira",
    secret=SECRET,
    project="PRI",
    key="PRI-1",
    finish_key="PRI-2",
    target_id="2",
    target_label="Done",
    missing_target="Missing",
    factory=build,
    min_rows=200,
    page_paths=("/search/jql",),
)
