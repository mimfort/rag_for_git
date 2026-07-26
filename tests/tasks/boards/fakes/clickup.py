"""Фейк доски ClickUp для общего contract-набора."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from reviewer.tasks.boards.clickup import ClickUpBoard
from tests.tasks.boards.fakes.base import (
    FakeState,
    ProviderAdapter,
    RecordingTransport,
    record,
    request_json,
)

SECRET = "pk_clickup_contract_secret"
LIST_ID = "901"
DEFAULT_STATUS = "к выполнению"
DONE_STATUS = "готово"
ATTACHMENT_URL = "https://attachments.clickup.com/att-1/spec.txt"

_PARENT_ID = "t1"
_CHILD_ID = "s1"
_FINISH_ID = "f1"
_CREATED_ID = "c1"

PARENT_KEY = f"PRI-{int(_PARENT_ID, 36)}"
FINISH_KEY = f"PRI-{int(_FINISH_ID, 36)}"


@dataclass
class State(FakeState):
    """Состояние фейка ClickUp: описание/статус закрываемой задачи и счётчик созданий."""

    finish_description: str = "## Закрываемая задача\n\nтело"
    finish_status: str = "в работе"
    created: int = 0


def _task(
    native: str,
    *,
    name: str,
    updated: str,
    markdown: str,
    parent: str | None = None,
    status: str = DEFAULT_STATUS,
    status_type: str = "open",
    attachments: tuple[dict, ...] = (),
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": native,
        "name": name,
        "description": name,
        "markdown_description": markdown,
        "status": {"status": status, "type": status_type, "orderindex": 0, "color": "#87909e"},
        "date_updated": updated,
        "url": f"https://app.clickup.com/t/{native}",
        "list": {"id": LIST_ID, "name": "Бэклог"},
    }
    if parent is not None:
        task["parent"] = parent
    if attachments:
        task["attachments"] = list(attachments)
    return task


def _parent_task() -> dict[str, Any]:
    return _task(
        _PARENT_ID,
        name="Родительская задача",
        updated="1784797200000",
        markdown="## Родительская задача\n\nСвязано с PRI-77",
        attachments=(
            {
                "id": "att-1",
                "title": "spec.txt",
                "url": ATTACHMENT_URL,
                "size": 24,
                "mimetype": "text/plain",
            },
        ),
    )


def _child_task() -> dict[str, Any]:
    return _task(
        _CHILD_ID,
        name="Подзадача",
        updated="1784797210000",
        markdown="## Подзадача",
        parent=_PARENT_ID,
    )


def _all_tasks() -> list[dict[str, Any]]:
    """150 задач: две страницы по 100 (первая — родитель с подзадачей и вложением)."""
    tasks = [_parent_task(), _child_task()]
    tasks.extend(
        _task(
            f"t{number}",
            name=f"Задача {number}",
            updated=f"17847972{number:05d}",
            markdown=f"## Задача {number}",
        )
        for number in range(2, 150)
    )
    return tasks


def _finish_task(state: State) -> dict[str, Any]:
    return _task(
        _FINISH_ID,
        name="Закрываемая задача",
        updated="1784797300000",
        markdown=state.finish_description,
        status=state.finish_status,
        status_type="custom",
    )


def _clickup_handler(
    state: State,
    *,
    error_status: int | None,
) -> Callable[[httpx.Request], httpx.Response]:
    tasks = _all_tasks()

    def handle(request: httpx.Request) -> httpx.Response:
        record(state, request)
        if error_status is not None:
            return httpx.Response(error_status, json={"token": SECRET})
        path = request.url.path
        method = request.method
        body = request_json(request)
        if method == "GET" and path == "/api/v2/user":
            return httpx.Response(200, json={"user": {"id": 11, "username": "robot"}})
        if method == "GET" and path == f"/api/v2/list/{LIST_ID}":
            return httpx.Response(
                200,
                json={
                    "id": LIST_ID,
                    "name": "Бэклог",
                    "statuses": [
                        {"status": DEFAULT_STATUS, "orderindex": 0, "type": "open"},
                        {"status": "в работе", "orderindex": 1, "type": "custom"},
                        {"status": DONE_STATUS, "orderindex": 2, "type": "closed"},
                    ],
                },
            )
        if method == "GET" and path == f"/api/v2/list/{LIST_ID}/task":
            page = int(request.url.params.get("page") or 0)
            chunk = tasks[page * 100 : page * 100 + 100]
            payload: dict[str, Any] = {"tasks": chunk}
            if len(chunk) < 100:
                payload["last_page"] = True
            return httpx.Response(200, json=payload)
        if method == "POST" and path == f"/api/v2/list/{LIST_ID}/task":
            state.created += 1
            return httpx.Response(
                200,
                json={
                    "id": _CREATED_ID,
                    "name": body.get("name") or "",
                    "status": {"status": body.get("status") or DEFAULT_STATUS, "type": "open"},
                    "url": f"https://app.clickup.com/t/{_CREATED_ID}",
                    "date_updated": "1784797400000",
                },
            )
        if method == "GET" and path == f"/api/v2/task/{_PARENT_ID}":
            return httpx.Response(200, json={**_parent_task(), "subtasks": [_child_task()]})
        if method == "GET" and path == f"/api/v2/task/{_FINISH_ID}":
            return httpx.Response(200, json=_finish_task(state))
        if method == "PUT" and path == f"/api/v2/task/{_FINISH_ID}":
            if "markdown_content" in body:
                state.finish_description = str(body["markdown_content"])
                return httpx.Response(200, json={"id": _FINISH_ID})
            if "status" in body:
                if body["status"] not in {DEFAULT_STATUS, "в работе", DONE_STATUS}:
                    return httpx.Response(400, json={"err": "Status not found"})
                state.finish_status = str(body["status"])
                return httpx.Response(200, json={"id": _FINISH_ID})
            return httpx.Response(400, json={"err": "unexpected body"})
        if path.endswith("/spec.txt"):
            return httpx.Response(200, text="Критерий из вложения")
        return httpx.Response(404, json={"err": "not found"})

    return handle


def build(
    *,
    state: State | None = None,
    forbidden: bool = False,
    error_status: int | None = None,
) -> tuple[ClickUpBoard, State]:
    """Собрать ClickUpBoard с транспортом через конструктор (без сети)."""
    state = state or State()
    status = error_status or (403 if forbidden else None)
    provider = ClickUpBoard(
        token=SECRET,
        list_id=LIST_ID,
        key_prefix="PRI",
        key_pattern=r"PRI-\d+",
        attachment_max_bytes=1000,
        attachment_timeout=1.0,
        attachment_store_chars=1000,
        transport=RecordingTransport(_clickup_handler(state, error_status=status), state),
        sleeper=lambda _seconds: None,
    )
    return provider, state


ADAPTER = ProviderAdapter(
    board_type="clickup",
    secret=SECRET,
    project="PRI",
    key=PARENT_KEY,
    finish_key=FINISH_KEY,
    target_id=DONE_STATUS,
    target_label=DONE_STATUS,
    missing_target="Нет такой цели",
    factory=build,
    min_rows=100,
    page_paths=("/task",),
)
