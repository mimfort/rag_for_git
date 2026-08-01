"""Фейк доски YouGile для общего contract-набора."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
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
    created_children: dict[str, dict[str, Any]] = field(default_factory=dict)
    tasks_by_column: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    columns_by_board: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {
        "board-1": [
            {"id": "open-id", "title": "Open", "boardId": "board-1"},
            {"id": "done-id", "title": "Done", "boardId": "board-1"},
        ],
    })
    parent_subtasks: dict[str, list[str]] = field(default_factory=dict)
    fail_created_reads: bool = False
    commit_then_timeout: bool = False


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


def _task_with_parent_subtasks(state: State, task: dict[str, Any]) -> dict[str, Any]:
    task = dict(task)
    if task["id"] in state.parent_subtasks:
        task["subtasks"] = list(state.parent_subtasks[task["id"]])
    return task


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
            board_id = params.get("boardId")
            columns = (
                state.columns_by_board.get(str(board_id), [])
                if board_id
                else [column for items in state.columns_by_board.values() for column in items]
            )
            offset = int(params.get("offset", "0"))
            limit = int(params.get("limit", "1000"))
            return httpx.Response(
                200,
                json={"content": columns[offset : offset + limit]},
            )
        if request.method == "GET" and path == "/tasks":
            column = params.get("columnId")
            offset = int(params.get("offset", "0"))
            if column == "open-id":
                tasks = [_task_with_parent_subtasks(state, _yougile_task(n))
                         for n in range(1, 1002)]
            else:
                tasks = []
            tasks.extend(
                _task_with_parent_subtasks(state, task)
                for task in state.tasks_by_column.get(str(column), [])
            )
            tasks.extend(
                _task_with_parent_subtasks(state, child)
                for child in state.created_children.values()
                if child.get("columnId") == column
            )
            return httpx.Response(200, json={"content": tasks[offset : offset + 1000]})
        if request.method == "GET" and path == "/tasks/sub-1":
            return httpx.Response(
                200,
                json={"id": "sub-1", "idTaskCommon": "ID-9", "title": "Подзадача"},
            )
        if request.method == "GET" and path == "/tasks/ID-1":
            return httpx.Response(200, json=_task_with_parent_subtasks(state, _yougile_task(1)))
        if request.method == "GET" and path == "/tasks/ID-2":
            return httpx.Response(
                200,
                json=_task_with_parent_subtasks(
                    state,
                    _yougile_task(
                        2,
                        description=state.task_description,
                        completed=state.task_completed,
                        column_id=state.task_column,
                    ),
                ),
            )
        if request.method == "GET" and path.startswith("/tasks/"):
            task_id = path.removeprefix("/tasks/")
            if task_id in state.created_children:
                if state.fail_created_reads:
                    return httpx.Response(500, json={})
                return httpx.Response(
                    200,
                    json=_task_with_parent_subtasks(state, state.created_children[task_id]),
                )
            if task_id in {"uuid-1", "uuid-2"}:
                return httpx.Response(
                    200,
                    json=_task_with_parent_subtasks(state, _yougile_task(int(task_id[-1]))),
                )
            for tasks in state.tasks_by_column.values():
                task = next(
                    (
                        item
                        for item in tasks
                        if task_id in {item.get("id"), item.get("idTaskCommon"),
                                       item.get("idTaskProject")}
                    ),
                    None,
                )
                if task is not None:
                    return httpx.Response(
                        200,
                        json=_task_with_parent_subtasks(state, task),
                    )
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
            uuid = "uuid-created" if state.created == 1 else f"uuid-created-{state.created}"
            payload = request_json(request)
            state.created_children[uuid] = {
                "id": uuid,
                "idTaskCommon": f"ID-{76 + state.created}",
                "idTaskProject": f"PRI-{76 + state.created}",
                "title": payload.get("title", ""),
                "description": payload.get("description", ""),
                "columnId": payload.get("columnId"),
                "subtasks": [],
                "timestamp": 2000 + state.created,
                "completed": False,
            }
            if state.commit_then_timeout:
                raise httpx.ReadTimeout("committed response timeout", request=request)
            return httpx.Response(200, json={"id": uuid})
        if request.method == "PUT" and path.startswith("/tasks/"):
            payload = request_json(request)
            if set(payload) == {"subtasks"}:
                parent_id = path.removeprefix("/tasks/")
                state.parent_subtasks[parent_id] = list(payload["subtasks"])
                for tasks in (*state.tasks_by_column.values(), state.created_children.values()):
                    for task in tasks:
                        if task.get("id") == parent_id:
                            task["subtasks"] = list(payload["subtasks"])
                return httpx.Response(200, json={})
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
