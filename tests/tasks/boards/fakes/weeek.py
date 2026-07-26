"""Фейк доски Weeek для общего contract-набора.

Listing (``GET /tm/tasks``) и точечный ``GET /tm/tasks/{id}`` отдают одну и ту же
задачу из состояния фейка, поэтому contract-проверка «один маппер для iter_raw и
fetch_one» сравнивает одинаковые payload'ы. Мутации (``PUT`` описания,
``/complete``, ``/board-column``) живут в ``State``, поэтому повторный ``finish``
видит уже дописанную PR-ссылку, уже закрытую задачу и уже нужную колонку.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from reviewer.tasks.boards.weeek import WeeekBoard
from tests.tasks.boards.fakes.base import (
    FakeState,
    ProviderAdapter,
    RecordingTransport,
    record,
    request_json,
)

SECRET = "weeek-contract-secret"
BASE_URL = "https://api.weeek.net/public/v1"
PREFIX = "WEEEK"
PROJECT_ID = 4
BOARD_ID = 6
TASKS = 101             # 2 страницы по perPage=100 → минимум два страничных запроса
FINISH_TASK = 2
CREATED_TASK = 77
COLUMNS = [
    {"id": 8, "name": "Backlog", "boardId": BOARD_ID},
    {"id": 9, "name": "Done", "boardId": BOARD_ID},
]


@dataclass
class State(FakeState):
    """Состояние фейка Weeek: изменяемые задачи и счётчик созданий."""

    tasks: dict[int, dict[str, Any]] = field(default_factory=dict)
    created: int = 0


def _task(number: int) -> dict[str, Any]:
    """Задача Weeek; у первой есть подзадача и вложение ``spec.txt``."""
    payload: dict[str, Any] = {
        "id": number,
        "parentId": None,
        "title": f"Задача {number}",
        "description": f"<p>Описание задачи {number}</p>",
        "type": "action",
        "priority": 1,
        "isCompleted": False,
        "isDeleted": False,
        "projectId": PROJECT_ID,
        "boardId": BOARD_ID,
        "boardColumnId": 8,
        "locations": [
            {"projectId": PROJECT_ID, "boardId": BOARD_ID, "boardColumnId": 8},
        ],
        "createdAt": "2026-07-20T08:00:00Z",
        "updatedAt": f"2026-07-23T09:{number % 60:02d}:00Z",
        "completedAt": None,
        "subTasks": [],
        "attachments": [],
    }
    if number == 1:
        payload["description"] = (
            "<p>Падает синк, см. WEEEK-42.</p><ul><li>проверить watermark</li></ul>"
        )
        payload["subTasks"] = [909]
        payload["attachments"] = [
            {
                "id": "att-1",
                "name": "spec.txt",
                "url": "https://files.weeek.net/spec.txt",
                "size": 24,
                "service": "weeek",
            },
        ]
    return payload


def _stateful(state: State, number: int) -> dict[str, Any]:
    """Задача из изменяемого состояния: PUT/complete/board-column видны следующему GET."""
    return state.tasks.setdefault(number, _task(number))


def _handler(
    state: State,
    *,
    error_status: int | None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        record(state, request)
        if error_status is not None:
            return httpx.Response(error_status, json={"token": SECRET})
        method, path = request.method, request.url.path.removeprefix("/public/v1")
        if method == "GET" and path == "/user/me":
            return httpx.Response(200, json={
                "success": True,
                "user": {
                    "id": "3e265f8a-5c6c-4169-a2b1-6182f10b712b",
                    "email": "robot@weeek.test",
                    "firstName": "Ревью",
                    "lastName": "Бот",
                },
            })
        if method == "GET" and path == "/tm/board-columns":
            return httpx.Response(200, json={"success": True, "boardColumns": COLUMNS})
        if method == "GET" and path == "/tm/tasks":
            offset = int(request.url.params.get("offset", "0"))
            per_page = int(request.url.params.get("perPage", "100"))
            numbers = list(range(1, TASKS + 1))[offset : offset + per_page]
            return httpx.Response(200, json={
                "success": True,
                "tasks": [_stateful(state, number) for number in numbers],
                "hasMore": offset + per_page < TASKS,
            })
        if method == "POST" and path == "/tm/tasks":
            body = request_json(request)
            state.created += 1
            created = _stateful(state, CREATED_TASK)
            created["title"] = body.get("title") or created["title"]
            created["description"] = body.get("description") or ""
            location = (body.get("locations") or [{}])[0]
            created["boardColumnId"] = location.get("boardColumnId")
            created["locations"] = [location]
            return httpx.Response(200, json={"success": True, "task": created})
        if method == "POST" and path.endswith("/complete"):
            number = int(path.removeprefix("/tm/tasks/").removesuffix("/complete"))
            _stateful(state, number)["isCompleted"] = True
            return httpx.Response(200, json={"success": True})
        if method == "POST" and path.endswith("/board-column"):
            number = int(path.removeprefix("/tm/tasks/").removesuffix("/board-column"))
            column_id = request_json(request).get("boardColumnId")
            task = _stateful(state, number)
            task["boardColumnId"] = column_id
            task["locations"] = [
                {"projectId": PROJECT_ID, "boardId": BOARD_ID, "boardColumnId": column_id},
            ]
            return httpx.Response(200, json={"success": True})
        if method in {"GET", "PUT"} and path.startswith("/tm/tasks/"):
            tail = path.removeprefix("/tm/tasks/")
            if not tail.isdigit():
                return httpx.Response(404, json={"success": False})
            number = int(tail)
            if number not in state.tasks and not (1 <= number <= TASKS):
                return httpx.Response(404, json={"success": False})
            task = _stateful(state, number)
            if method == "PUT":
                body = request_json(request)
                if "description" in body:
                    task["description"] = str(body["description"])
            return httpx.Response(200, json={"success": True, "task": task})
        if method == "GET" and path == "/ws/attachments/att-1":
            return httpx.Response(200, json={
                "success": True,
                "data": {"url": "https://files.weeek.net/spec.txt"},
            })
        if method == "GET" and request.url.path == "/spec.txt":
            return httpx.Response(200, text="Критерий из вложения")
        return httpx.Response(404, json={"success": False})

    return handle


def build(
    *,
    state: State | None = None,
    forbidden: bool = False,
    error_status: int | None = None,
) -> tuple[WeeekBoard, State]:
    """Собрать WeeekBoard с транспортом через конструктор (без сети)."""
    state = state or State()
    status = error_status or (403 if forbidden else None)
    provider = WeeekBoard(
        api_token=SECRET,
        api_base=BASE_URL,
        project_id=str(PROJECT_ID),
        board_id=str(BOARD_ID),
        key_prefix=PREFIX,
        key_pattern=r"WEEEK-\d+",
        url_template="https://app.weeek.net/ws/1/task/{code}",
        attachment_max_bytes=1000,
        attachment_timeout=1.0,
        attachment_store_chars=1000,
        transport=RecordingTransport(_handler(state, error_status=status), state),
        sleeper=lambda _: None,
    )
    return provider, state


ADAPTER = ProviderAdapter(
    board_type="weeek",
    secret=SECRET,
    project=PREFIX,
    key=f"{PREFIX}-1",
    finish_key=f"{PREFIX}-{FINISH_TASK}",
    target_id="9",
    target_label="Done",
    missing_target="Missing",
    factory=build,
    min_rows=100,
    page_paths=("/tm/tasks",),
)
