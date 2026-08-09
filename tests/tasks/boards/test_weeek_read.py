"""Чтение доски Weeek: пагинация offset/perPage, маппинг RawTask, лимит, fetch_one."""
from __future__ import annotations

import dataclasses
from typing import Any

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.weeek import WeeekBoard

TOKEN = "weeek-secret-token"
BASE = "https://api.weeek.net/public/v1"
COLUMNS = {
    "success": True,
    "boardColumns": [
        {"id": 8, "name": "Backlog", "boardId": 6},
        {"id": 9, "name": "Done", "boardId": 6},
    ],
}


def _board(handler, **kwargs) -> WeeekBoard:
    params: dict[str, Any] = {
        "api_token": TOKEN,
        "project_id": "4",
        "board_id": "6",
        "key_prefix": "WEEEK",
        "key_pattern": r"WEEEK-\d+",
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _seconds: None,
    }
    params.update(kwargs)
    return WeeekBoard(**params)


def _task(number: int, **overrides: Any) -> dict[str, Any]:
    task = {
        "id": number,
        "parentId": None,
        "title": f"Задача {number}",
        "description": f"<p>Описание задачи {number}</p>",
        "type": "action",
        "priority": None,
        "isCompleted": False,
        "isDeleted": False,
        "authorId": "3e265f8a-5c6c-4169-a2b1-6182f10b712b",
        "assignees": [],
        "projectId": 4,
        "boardId": 6,
        "boardColumnId": 8,
        "locations": [{"projectId": 4, "boardId": 6, "boardColumnId": 8}],
        "createdAt": "2026-07-20T08:00:00Z",
        "updatedAt": "2026-07-23T09:15:00Z",
        "completedAt": None,
        "tags": [],
        "subscribers": [],
        "subTasks": [],
        "timeEntries": [],
        "customFields": [],
        "attachments": [],
    }
    task.update(overrides)
    return task


def _pages_handler(seen: list[httpx.Request]):
    """Две страницы задач (100 + 1) и список колонок доски."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/tm/board-columns"):
            return httpx.Response(200, json=COLUMNS)
        offset = int(request.url.params.get("offset", "0"))
        if offset == 0:
            tasks = [_task(number) for number in range(1, 101)]
            return httpx.Response(200, json={"success": True, "tasks": tasks, "hasMore": True})
        return httpx.Response(
            200,
            json={"success": True, "tasks": [_task(101)], "hasMore": False},
        )

    return handler


def test_pagination_walks_offset_pages_with_exact_params_and_auth() -> None:
    seen: list[httpx.Request] = []

    rows = list(_board(_pages_handler(seen)).iter_raw("WEEEK", None))

    task_calls = [request for request in seen if request.url.path.endswith("/tm/tasks")]
    assert len(rows) == 101
    assert len(task_calls) == 2
    assert task_calls[0].url.path == "/public/v1/tm/tasks"
    assert dict(task_calls[0].url.params) == {
        "projectId": "4",
        "boardId": "6",
        "perPage": "100",
        "offset": "0",
    }
    assert dict(task_calls[1].url.params)["offset"] == "100"
    assert task_calls[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert task_calls[0].headers["Accept"] == "application/json"


def test_board_columns_are_read_once_for_status_titles() -> None:
    seen: list[httpx.Request] = []

    rows = list(_board(_pages_handler(seen)).iter_raw("WEEEK", None))

    column_calls = [request for request in seen if request.url.path.endswith("/tm/board-columns")]
    assert len(column_calls) == 1
    assert dict(column_calls[0].url.params) == {"boardId": "6"}
    assert rows[0].status == "Backlog"


def test_raw_task_maps_synthesized_key_native_id_and_epoch_ms() -> None:
    payload = {
        "success": True,
        "tasks": [
            _task(
                7,
                isCompleted=True,
                parentId=3,
                subTasks=[9, 10],
                updatedAt="2026-07-23T09:15:00Z",
            )
        ],
        "hasMore": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tm/board-columns"):
            return httpx.Response(200, json=COLUMNS)
        return httpx.Response(200, json=payload)

    row = next(iter(_board(handler).iter_raw(None, None)))

    assert row.key == "WEEEK-7"
    assert row.project_code == "WEEEK-7"
    assert row.board_id == "7"
    assert row.archived is None
    assert row.terminal is True
    assert row.subtask_ids == ["9", "10"]
    assert row.timestamp == 1784798100000  # 2026-07-23T09:15:00Z
    assert row.provider_data["parent_id"] == 3
    assert row.provider_data["board_column_id"] == 8


def test_unparsable_updated_at_is_unknown_without_raising() -> None:
    payload = {"success": True, "tasks": [_task(1, updatedAt="никогда")], "hasMore": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tm/board-columns"):
            return httpx.Response(200, json=COLUMNS)
        return httpx.Response(200, json=payload)

    row = next(iter(_board(handler).iter_raw(None, None)))

    assert row.timestamp is None


def test_lifecycle_uses_is_completed_but_not_is_deleted_as_archive() -> None:
    missing = _task(3, isDeleted=True)
    missing.pop("isCompleted")
    payload = {
        "success": True,
        "tasks": [
            _task(1, isCompleted=True, isDeleted=True),
            _task(2, isCompleted=False),
            missing,
        ],
        "hasMore": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tm/board-columns"):
            return httpx.Response(200, json=COLUMNS)
        return httpx.Response(200, json=payload)

    rows = list(_board(handler).iter_raw(None, None))

    assert [row.terminal for row in rows] == [True, False, None]
    assert [row.archived for row in rows] == [None, None, None]
    assert rows[0].provider_data["is_deleted"] is True


def test_limit_stops_the_walk_before_the_second_page() -> None:
    seen: list[httpx.Request] = []

    rows = list(_board(_pages_handler(seen)).iter_raw("WEEEK", 5))

    task_calls = [request for request in seen if request.url.path.endswith("/tm/tasks")]
    assert len(rows) == 5
    assert len(task_calls) == 1


def test_numeric_scope_replaces_the_configured_project_filter() -> None:
    seen: list[httpx.Request] = []

    rows = list(_board(_pages_handler(seen)).iter_raw("12", 1))

    task_calls = [request for request in seen if request.url.path.endswith("/tm/tasks")]
    assert dict(task_calls[0].url.params)["projectId"] == "12"
    assert len(rows) == 1


def test_prefix_scope_filters_out_tasks_of_another_project() -> None:
    seen: list[httpx.Request] = []

    rows = list(_board(_pages_handler(seen)).iter_raw("OTHER", None))

    assert rows == []


def test_fetch_one_uses_the_same_mapper_as_iter_raw() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tm/board-columns"):
            return httpx.Response(200, json=COLUMNS)
        if request.url.path.endswith("/tm/tasks/1"):
            return httpx.Response(200, json={"success": True, "task": _task(1)})
        return httpx.Response(
            200,
            json={"success": True, "tasks": [_task(1)], "hasMore": False},
        )

    provider = _board(handler)
    row = next(iter(provider.iter_raw(None, None)))
    one = provider.fetch_one("WEEEK-1")

    assert one is not None
    assert dataclasses.asdict(one) == dataclasses.asdict(row)


def test_fetch_one_returns_none_for_a_missing_task() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"success": False})

    assert _board(handler).fetch_one("WEEEK-404") is None


def test_fetch_one_returns_none_for_a_key_without_a_numeric_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - не зовётся
        raise AssertionError("сеть не должна дёргаться на невалидном ключе")

    assert _board(handler).fetch_one("WEEEK-нет") is None


USER = {
    "success": True,
    "user": {
        "id": "3e265f8a-5c6c-4169-a2b1-6182f10b712b",
        "email": "bot@weeek.test",
        "firstName": "Ревью",
        "lastName": "Бот",
    },
}


def _identity_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/user/me"):
        return httpx.Response(200, json=USER)
    if request.url.path.endswith("/tm/board-columns"):
        return httpx.Response(200, json=COLUMNS)
    return httpx.Response(404, json={"success": False})


def test_validate_connection_reports_identity_scope_and_capabilities() -> None:
    result = _board(_identity_handler).validate_connection("WEEEK")

    assert set(result) == {"status", "identity", "project", "capabilities", "warnings"}
    assert result["status"] == "ok"
    assert result["identity"] == {
        "id": "3e265f8a-5c6c-4169-a2b1-6182f10b712b",
        "email": "bot@weeek.test",
        "name": "Ревью Бот",
    }
    assert result["project"] == "4"
    assert result["capabilities"] == {"read": True, "create": True, "finish": True}
    assert result["warnings"] == []
    assert TOKEN not in repr(result)


def test_validate_connection_warns_when_project_and_board_are_missing() -> None:
    result = _board(_identity_handler, project_id="", board_id="").validate_connection(None)

    assert result["status"] == "ok"
    assert result["capabilities"]["create"] is False
    assert result["project"] is None
    assert len(result["warnings"]) == 2


def test_validate_connection_maps_forbidden_and_missing_to_categories() -> None:
    for status, category in ((403, "permission"), (404, "not_found")):
        def handler(request: httpx.Request, status: int = status) -> httpx.Response:
            return httpx.Response(status, json={"token": TOKEN})

        with pytest.raises(BoardProviderError) as exc_info:
            _board(handler).validate_connection("WEEEK")

        assert exc_info.value.category == category
        assert TOKEN not in f"{exc_info.value}{exc_info.value!r}"
