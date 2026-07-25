"""Фейк доски YouGile для общего contract-набора."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from reviewer.tasks.boards.yougile import YougileBoard
from tests.tasks.boards.fakes.base import (
    FakeState,
    ProviderAdapter,
    RecordingTransport,
    record,
    request_json,
)

SECRET = "yougile-contract-secret"
API_BASE = "https://yougile.test/api-v2"


@dataclass
class State(FakeState):
    """Состояние фейка YouGile: изменяемые поля задачи ID-2 и счётчик созданий."""

    task_description: str = "<p>Описание PRI-2</p>"
    task_completed: bool = False
    task_column: str = "open-id"
    created: int = 0


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
    state: State,
    *,
    error_status: int | None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        record(state, request)
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
            return httpx.Response(
                200,
                json={"id": "open-id", "title": "Open", "boardId": "board-1"},
            )
        if request.method == "GET" and path == "/columns/done-id":
            return httpx.Response(
                200,
                json={"id": "done-id", "title": "Done", "boardId": "board-1"},
            )
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
            payload = request_json(request)
            state.task_description = payload.get("description", state.task_description)
            state.task_completed = payload.get("completed", state.task_completed)
            state.task_column = payload.get("columnId", state.task_column)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    return handle


def build(
    *,
    state: State | None = None,
    forbidden: bool = False,
    error_status: int | None = None,
) -> tuple[YougileBoard, State]:
    """Собрать YougileBoard на записывающем MockTransport (без сети)."""
    state = state or State()
    status = error_status or (403 if forbidden else None)
    provider = YougileBoard(
        api_key=SECRET,
        api_base=API_BASE,
        key_pattern=r"PRI-\d+",
        url_template="https://yougile.test/#task/{code}",
    )
    provider._client.close()  # type: ignore[attr-defined]
    provider._client = httpx.Client(  # type: ignore[attr-defined]
        base_url=API_BASE,
        transport=RecordingTransport(_yougile_handler(state, error_status=status), state),
    )
    return provider, state


ADAPTER = ProviderAdapter(
    board_type="yougile",
    secret=SECRET,
    project="PRI",
    key="ID-1",
    finish_key="ID-2",
    target_id="done-id",
    target_label="Done",
    missing_target="Missing",
    factory=build,
    min_rows=1000,
    page_paths=("/tasks",),
)
