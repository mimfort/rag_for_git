"""Фейк доски Asana для общего contract-набора."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from reviewer.tasks.boards.asana import AsanaBoard
from tests.tasks.boards.fakes.base import (
    FakeState,
    ProviderAdapter,
    RecordingTransport,
    record,
    request_json,
)

SECRET = "asana-contract-secret"
API_BASE = "https://app.asana.test/api/1.0"
PROJECT_GID = "1201234567890"
FILE_URL = "https://asana-files.test/private/spec.txt?sig=short-lived"
PAGE = 100
TOTAL = 101
SECTIONS = [{"gid": "5001", "name": "Todo"}, {"gid": "5003", "name": "Done"}]


def gid(number: int) -> str:
    """Длинный числовой gid задачи, как у настоящей Asana."""
    return f"120765432{number:04d}"


@dataclass
class State(FakeState):
    """Состояние фейка Asana: изменяемые поля задачи 2 и счётчик созданий."""

    html_notes: str = "<body>Описание <em>задачи</em></body>"
    completed: bool = False
    section: dict = field(default_factory=lambda: dict(SECTIONS[0]))
    created: int = 0


def _task(number: int, **over: object) -> dict[str, Any]:
    task: dict[str, Any] = {
        "gid": gid(number),
        "name": f"Задача {number}",
        "html_notes": (
            "<body><h1>Проблема</h1>Клиент видит <strong>ошибку</strong>"
            "<ul><li>шаг один</li></ul></body>"
        ),
        "notes": f"Описание {number}",
        "completed": False,
        "modified_at": f"2026-07-23T09:{number % 60:02d}:00.000Z",
        "permalink_url": f"https://app.asana.test/0/{PROJECT_GID}/{gid(number)}",
        "num_subtasks": 1 if number == 1 else 0,
        "memberships": [
            {"project": {"gid": PROJECT_GID}, "section": dict(SECTIONS[0])}
        ],
    }
    task.update(over)
    return task


def _state_task(state: State) -> dict[str, Any]:
    return _task(
        2,
        html_notes=state.html_notes,
        completed=state.completed,
        memberships=[{"project": {"gid": PROJECT_GID}, "section": dict(state.section)}],
    )


def _asana_handler(
    state: State,
    *,
    error_status: int | None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        record(state, request)
        if error_status is not None:
            return httpx.Response(error_status, json={"token": SECRET})
        path = request.url.path.removeprefix("/api/1.0")
        method = request.method
        if request.url.host == "asana-files.test":
            return httpx.Response(200, text="Критерий из вложения")
        if method == "GET" and path == "/users/me":
            return httpx.Response(200, json={"data": {"gid": "9876", "name": "Robot"}})
        if method == "GET" and path == f"/projects/{PROJECT_GID}":
            return httpx.Response(
                200,
                json={"data": {"gid": PROJECT_GID, "name": "Reviewer"}},
            )
        if method == "GET" and path == f"/projects/{PROJECT_GID}/sections":
            return httpx.Response(200, json={"data": SECTIONS, "next_page": None})
        if method == "GET" and path == "/tasks":
            start = PAGE if request.url.params.get("offset") else 0
            rows = [_task(number) for number in range(1, TOTAL + 1)][start : start + PAGE]
            payload: dict[str, Any] = {"data": rows, "next_page": None}
            if start == 0:
                payload["next_page"] = {
                    "offset": "page-2",
                    "path": "/tasks?offset=page-2",
                    "uri": f"{API_BASE}/tasks?offset=page-2",
                }
            return httpx.Response(200, json=payload)
        if method == "GET" and path == f"/tasks/{gid(1)}/subtasks":
            return httpx.Response(
                200,
                json={
                    "data": [{"gid": "9001", "name": "Подзадача", "completed": False}],
                    "next_page": None,
                },
            )
        if method == "GET" and path == "/attachments":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "gid": "7001",
                            "name": "spec.txt",
                            "size": 24,
                            "download_url": FILE_URL,
                            "permanent_url": "https://app.asana.test/app/asana/-/asset",
                        }
                    ],
                    "next_page": None,
                },
            )
        if method == "GET" and path == f"/tasks/{gid(1)}":
            return httpx.Response(200, json={"data": _task(1)})
        if method == "GET" and path in {f"/tasks/{gid(2)}", f"/tasks/{gid(77)}"}:
            return httpx.Response(200, json={"data": _state_task(state)})
        if method == "POST" and path == "/tasks":
            state.created += 1
            return httpx.Response(201, json={"data": _task(77)})
        if method == "PUT" and path in {f"/tasks/{gid(2)}", f"/tasks/{gid(77)}"}:
            data = request_json(request).get("data") or {}
            state.html_notes = data.get("html_notes", state.html_notes)
            state.completed = bool(data.get("completed", state.completed))
            return httpx.Response(200, json={"data": _state_task(state)})
        if method == "POST" and path == "/sections/5003/addTask":
            state.section = dict(SECTIONS[1])
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(404, json={})

    return handle


def build(
    *,
    state: State | None = None,
    forbidden: bool = False,
    error_status: int | None = None,
) -> tuple[AsanaBoard, State]:
    """Собрать AsanaBoard с записывающим транспортом через конструктор (без сети)."""
    state = state or State()
    status = error_status or (403 if forbidden else None)
    provider = AsanaBoard(
        access_token=SECRET,
        api_base=API_BASE,
        project_gid=PROJECT_GID,
        key_prefix="ASN",
        key_pattern=r"ASN-\d+",
        url_template="https://app.asana.test/task/{code}",
        attachment_max_bytes=1000,
        attachment_timeout=1.0,
        attachment_store_chars=1000,
        transport=RecordingTransport(_asana_handler(state, error_status=status), state),
        sleeper=lambda _seconds: None,
    )
    return provider, state


ADAPTER = ProviderAdapter(
    board_type="asana",
    secret=SECRET,
    project="ASN",
    key=f"ASN-{gid(1)}",
    finish_key=f"ASN-{gid(2)}",
    target_id="5003",
    target_label="Done",
    missing_target="Missing",
    factory=build,
    min_rows=PAGE,
    page_paths=("/tasks",),
)
