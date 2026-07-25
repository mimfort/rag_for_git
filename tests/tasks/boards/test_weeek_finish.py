"""Закрытие задачи Weeek: PR-ссылка в описании, isCompleted, колонка, идемпотентность."""
from __future__ import annotations

import json
from typing import Any

import httpx

from reviewer.tasks.boards.weeek import WeeekBoard

TOKEN = "weeek-secret-token"
PR_URL = "https://github.test/pull/7"
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
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _seconds: None,
    }
    params.update(kwargs)
    return WeeekBoard(**params)


class _Board:
    """Задача в памяти: PUT/complete/board-column видны следующему GET."""

    def __init__(self, **overrides: Any) -> None:
        self.task: dict[str, Any] = {
            "id": 2,
            "title": "Задача 2",
            "description": "<p>Описание</p>",
            "isCompleted": False,
            "boardColumnId": 8,
            "locations": [{"projectId": 4, "boardId": 6, "boardColumnId": 8}],
            "updatedAt": "2026-07-23T09:15:00Z",
            "subTasks": [],
            "attachments": [],
        }
        self.task.update(overrides)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.failing: tuple[tuple[str, str], ...] = ()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode()) if request.content else {}
        self.calls.append((request.method, path, body))
        if any(request.method == method and path.endswith(s) for method, s in self.failing):
            return httpx.Response(500, json={"token": TOKEN})
        if path.endswith("/tm/board-columns"):
            return httpx.Response(200, json=COLUMNS)
        if path.endswith("/tm/tasks/2/complete"):
            self.task["isCompleted"] = True
            return httpx.Response(200, json={"success": True})
        if path.endswith("/tm/tasks/2/board-column"):
            self.task["boardColumnId"] = body["boardColumnId"]
            self.task["locations"] = [
                {"projectId": 4, "boardId": 6, "boardColumnId": body["boardColumnId"]}
            ]
            return httpx.Response(200, json={"success": True})
        if path.endswith("/tm/tasks/2"):
            if request.method == "PUT":
                self.task["description"] = body["description"]
                return httpx.Response(200, json={"success": True, "task": self.task})
            return httpx.Response(200, json={"success": True, "task": self.task})
        return httpx.Response(404, json={"success": False})

    def writes(self) -> list[tuple[str, str]]:
        return [
            (method, path.rsplit("/public/v1", 1)[-1])
            for method, path, _body in self.calls
            if method in {"POST", "PUT"}
        ]


def test_finish_appends_pr_link_completes_task_and_moves_the_column() -> None:
    board = _Board()

    result = _board(board.handler).finish(
        "WEEEK-2",
        PR_URL,
        note="Проверено",
        target="9",
    )

    assert result["pr_link_added"] is True
    assert result["done_set"] is True
    assert result["column_moved"] is True
    assert result["already_closed"] is False
    assert result["target_resolved"] == "Done"
    assert result["board_id"] == "2"
    assert result["warnings"] == []
    assert board.writes() == [
        ("PUT", "/tm/tasks/2"),
        ("POST", "/tm/tasks/2/board-column"),
        ("POST", "/tm/tasks/2/complete"),
    ]
    assert "<!-- reviewer:pr-link -->" in board.task["description"]
    assert PR_URL in board.task["description"]
    assert "Проверено" in board.task["description"]
    assert board.task["boardColumnId"] == 9


def test_finish_is_idempotent_on_a_repeated_call() -> None:
    board = _Board()
    provider = _board(board.handler)

    first = provider.finish("WEEEK-2", PR_URL, note="Проверено", target="9")
    board.calls.clear()
    second = provider.finish("WEEEK-2", PR_URL, note="Проверено", target="9")

    assert first["already_closed"] is False
    assert second["pr_link_added"] is False
    assert second["done_set"] is False
    assert second["column_moved"] is False
    assert second["already_closed"] is True
    assert board.writes() == []


def test_already_closed_task_with_the_link_writes_nothing() -> None:
    board = _Board(
        isCompleted=True,
        boardColumnId=9,
        locations=[{"projectId": 4, "boardId": 6, "boardColumnId": 9}],
        description=f'<p>PR: <a href="{PR_URL}">{PR_URL}</a></p>',
    )

    result = _board(board.handler).finish("WEEEK-2", PR_URL, target="9")

    assert result["already_closed"] is True
    assert board.writes() == []


def test_mark_done_false_writes_only_the_link() -> None:
    board = _Board()

    result = _board(board.handler).finish("WEEEK-2", PR_URL, mark_done=False)

    assert result["pr_link_added"] is True
    assert result["done_set"] is False
    assert board.writes() == [("PUT", "/tm/tasks/2")]
    assert board.task["isCompleted"] is False


def test_note_and_url_are_html_escaped_in_the_description() -> None:
    board = _Board()

    _board(board.handler).finish(
        "WEEEK-2",
        "https://github.test/pull/7?a=1&b=2",
        note="<script>alert(1)</script>",
    )

    assert "<script>" not in board.task["description"]
    assert "&lt;script&gt;" in board.task["description"]
    assert "a=1&amp;b=2" in board.task["description"]


def test_unknown_target_warns_and_still_completes_the_task() -> None:
    board = _Board()

    result = _board(board.handler).finish("WEEEK-2", PR_URL, target="Missing")

    assert result["done_set"] is True
    assert result["column_moved"] is False
    assert result["target_resolved"] is None
    assert any("Missing" in warning for warning in result["warnings"])


def test_failed_description_update_is_a_warning_not_an_exception() -> None:
    board = _Board()
    board.failing = (("PUT", "/tm/tasks/2"),)

    result = _board(board.handler).finish("WEEEK-2", PR_URL, target="9")

    assert result["pr_link_added"] is False
    assert result["done_set"] is True
    assert result["warnings"]
    assert TOKEN not in repr(result)


def test_failed_completion_is_a_warning_and_keeps_the_link() -> None:
    board = _Board()
    board.failing = (("POST", "/complete"),)

    result = _board(board.handler).finish("WEEEK-2", PR_URL)

    assert result["pr_link_added"] is True
    assert result["done_set"] is False
    assert result["already_closed"] is False
    assert result["warnings"]
