"""Фейк доски Yandex Tracker для общего contract-набора.

Отдаёт две страницы ``POST /v3/issues/_search`` (100 + 1 задача), у первой задачи есть
подзадача и вложение ``spec.txt``, а описание — в YFM, чтобы проверялась конвертация в
markdown. Состояние (описание и статус ``TREK-2``/``TREK-77``) живёт в ``State``, поэтому
``finish`` идемпотентен по-настоящему: второй вызов видит уже дописанную PR-ссылку и
уже выполненный переход.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from reviewer.tasks.boards.yandex_tracker import YandexTrackerBoard
from tests.tasks.boards.fakes.base import (
    FakeState,
    ProviderAdapter,
    RecordingTransport,
    record,
    request_json,
)

SECRET = "yandex-tracker-contract-secret"
API_BASE = "https://api.tracker.yandex.net/v3"
PAGE = 100
TOTAL = 101

_STATUSES = [
    {"id": 1, "key": "open", "name": "Открыт", "type": "new"},
    {"id": 3, "key": "closed", "name": "Закрыт", "type": "done"},
]
_TRANSITIONS = [
    {"id": "close", "display": "Закрыть", "to": {"id": "3", "key": "closed", "display": "Закрыт"}}
]


@dataclass
class State(FakeState):
    """Состояние фейка Трекера: описание/статус изменяемой задачи и счётчик созданий."""

    description: str = "Описание TREK-2"
    status: dict = field(
        default_factory=lambda: {"id": "1", "key": "open", "display": "Открыт"}
    )
    created: int = 0


def _issue(number: int) -> dict[str, Any]:
    """Задача Трекера как её отдаёт ``_search`` с ``expand=attachments``."""
    return {
        "self": f"{API_BASE}/issues/TREK-{number}",
        "id": f"6000{number}",
        "key": f"TREK-{number}",
        "summary": f"Задача {number}",
        "description": (
            f"Описание TREK-{number} ((https://wiki.test/spec Спека))"
            if number != 1
            else "Описание TREK-1 ((https://wiki.test/spec Спека))\n\n%%(python)\nprint(1)\n%%"
        ),
        "status": {"id": "1", "key": "open", "display": "Открыт"},
        "queue": {"id": "3", "key": "TREK", "display": "Trek"},
        "updatedAt": f"2026-07-23T09:{number % 60:02d}:12.347+0000",
        "attachments": [
            {
                "id": "1",
                "name": "spec.txt",
                "content": f"{API_BASE}/issues/TREK-1/attachments/1/spec.txt",
                "mimetype": "text/plain",
                "size": 24,
            }
        ]
        if number == 1
        else [],
    }


def _stateful_issue(state: State, number: int) -> dict[str, Any]:
    issue = _issue(number)
    issue["description"] = state.description
    issue["status"] = state.status
    return issue


def _handler(state: State, *, error_status: int | None) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        record(state, request)
        if error_status is not None:
            return httpx.Response(error_status, json={"token": SECRET})
        method, path = request.method, request.url.path
        if method == "POST" and path == "/v3/issues/_search":
            page = int(request.url.params.get("page", 1))
            per_page = int(request.url.params.get("perPage", PAGE))
            issues = [_issue(number) for number in range(1, TOTAL + 1)]
            start = (page - 1) * per_page
            return httpx.Response(200, json=issues[start : start + per_page])
        if method == "GET" and path.endswith("/links"):
            return httpx.Response(
                200,
                json=[
                    {
                        "type": {
                            "id": "subtask",
                            "inward": "Подзадача",
                            "outward": "Родительская задача",
                        },
                        "direction": "outward",
                        "object": {"key": "TREK-9", "display": "Подзадача"},
                    }
                ],
            )
        if method == "GET" and path.endswith("/transitions"):
            return httpx.Response(200, json=_TRANSITIONS)
        if method == "POST" and path.endswith("/_execute"):
            state.status = {"id": "3", "key": "closed", "display": "Закрыт"}
            return httpx.Response(200, json=[{"status": state.status}])
        if method == "GET" and path == "/v3/issues/TREK-1":
            return httpx.Response(200, json=_issue(1))
        if method == "GET" and path in {"/v3/issues/TREK-2", "/v3/issues/TREK-77"}:
            number = 2 if path.endswith("TREK-2") else 77
            return httpx.Response(200, json=_stateful_issue(state, number))
        if method == "PATCH" and path in {"/v3/issues/TREK-2", "/v3/issues/TREK-77"}:
            state.description = str(request_json(request).get("description") or "")
            return httpx.Response(200, json={"key": path.rsplit("/", 1)[-1]})
        if method == "POST" and path == "/v3/issues":
            state.created += 1
            return httpx.Response(
                201,
                json={
                    "id": "60077",
                    "key": "TREK-77",
                    "status": {"id": "1", "key": "open", "display": "Открыт"},
                },
            )
        if method == "GET" and path == "/v3/statuses":
            return httpx.Response(200, json=_STATUSES)
        if method == "GET" and path == "/v3/queues":
            return httpx.Response(200, json=[{"id": 3, "key": "TREK", "name": "Trek"}])
        if method == "GET" and path == "/v3/queues/TREK":
            return httpx.Response(200, json={"id": 3, "key": "TREK", "name": "Trek"})
        if method == "GET" and path == "/v3/myself":
            return httpx.Response(
                200,
                json={"self": f"{API_BASE}/users/11", "uid": 11, "login": "robot",
                      "display": "Робот"},
            )
        if method == "GET" and path.endswith("/spec.txt"):
            return httpx.Response(200, text="Критерий из вложения")
        return httpx.Response(404, json={})

    return handle


def build(
    *,
    state: State | None = None,
    forbidden: bool = False,
    error_status: int | None = None,
) -> tuple[YandexTrackerBoard, State]:
    """Собрать YandexTrackerBoard с транспортом через конструктор (без сети)."""
    state = state or State()
    status = error_status or (403 if forbidden else None)
    provider = YandexTrackerBoard(
        token=SECRET,
        org_id="contract-org",
        queue="TREK",
        key_pattern=r"TREK-\d+",
        attachment_max_bytes=1000,
        attachment_timeout=1.0,
        attachment_store_chars=1000,
        transport=RecordingTransport(_handler(state, error_status=status), state),
        sleeper=lambda _: None,
    )
    return provider, state


ADAPTER = ProviderAdapter(
    board_type="yandex_tracker",
    secret=SECRET,
    project="TREK",
    key="TREK-1",
    finish_key="TREK-2",
    target_id="closed",
    target_label="Закрыт",
    missing_target="Missing",
    factory=build,
    min_rows=PAGE,
    page_paths=("/issues/_search",),
)
