"""Закрытие задачи ClickUp: идемпотентная PR-ссылка + смена статуса на done-цель."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import pytest

from reviewer.tasks.boards.clickup import ClickUpBoard
from reviewer.tasks.boards.errors import BoardProviderError

TOKEN = "pk_clickup_finish_secret"
PR_URL = "https://github.test/pull/7"
KEY = f"PRI-{int('2kv', 36)}"


@dataclass
class Board:
    """Состояние одной задачи ClickUp в памяти фейка."""

    description: str = "## Задача\n\nтело"
    status: str = "в работе"
    calls: list[tuple[str, str, dict]] = field(default_factory=list)
    status_error: int | None = None
    description_error: int | None = None


def _handler(board: Board):
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else {}
        board.calls.append((request.method, request.url.path, body))
        if request.url.path != "/api/v2/task/2kv":
            return httpx.Response(404, json={"err": "not found"})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "2kv",
                    "name": "Задача",
                    "markdown_description": board.description,
                    "status": {"status": board.status, "type": "custom"},
                    "date_updated": "1784797200000",
                    "url": "https://app.clickup.com/t/2kv",
                },
            )
        if request.method == "PUT" and "markdown_content" in body:
            if board.description_error is not None:
                return httpx.Response(board.description_error, json={"token": TOKEN})
            board.description = body["markdown_content"]
            return httpx.Response(200, json={"id": "2kv"})
        if request.method == "PUT" and "status" in body:
            if board.status_error is not None:
                return httpx.Response(board.status_error, json={"token": TOKEN})
            if body["status"] not in {"в работе", "готово"}:
                return httpx.Response(400, json={"err": "Status not found"})
            board.status = body["status"]
            return httpx.Response(200, json={"id": "2kv"})
        return httpx.Response(400, json={"err": "unexpected body"})

    return handle


def _board(board: Board, **kwargs) -> ClickUpBoard:
    params: dict = {
        "token": TOKEN,
        "list_id": "901",
        "key_prefix": "PRI",
        "key_pattern": r"PRI-\d+",
        "transport": httpx.MockTransport(_handler(board)),
        "sleeper": lambda _: None,
    }
    params.update(kwargs)
    return ClickUpBoard(**params)


def test_finish_appends_the_pr_link_and_sets_the_done_target() -> None:
    state = Board()
    board = _board(state)

    result = board.finish(KEY, PR_URL, note="Проверено", target="готово")

    assert result["key"] == KEY
    assert result["board_id"] == "2kv"
    assert result["pr_link_added"] is True
    assert result["done_set"] is True
    assert result["already_closed"] is False
    assert result["warnings"] == []
    assert state.description.endswith(f"PR: {PR_URL}\n\nПроверено")
    assert state.status == "готово"
    board.close()


def test_finish_is_idempotent_on_the_second_call() -> None:
    state = Board()
    board = _board(state)

    first = board.finish(KEY, PR_URL, note="Проверено", target="готово")
    writes_after_first = sum(1 for call in state.calls if call[0] == "PUT")
    second = board.finish(KEY, PR_URL, note="Проверено", target="готово")

    assert first["pr_link_added"] is True
    assert first["done_set"] is True
    assert second["pr_link_added"] is False
    assert second["done_set"] is False
    assert second["already_closed"] is True
    assert second["warnings"] == []
    assert sum(1 for call in state.calls if call[0] == "PUT") == writes_after_first
    board.close()


def test_finish_without_a_target_warns_and_leaves_the_status_alone() -> None:
    state = Board()
    board = _board(state)

    result = board.finish(KEY, PR_URL, target=None)

    assert result["pr_link_added"] is True
    assert result["done_set"] is False
    assert result["already_closed"] is False
    assert any("target" in warning for warning in result["warnings"])
    assert state.status == "в работе"
    board.close()


def test_unknown_target_warns_but_keeps_the_pr_link() -> None:
    state = Board()
    board = _board(state)

    result = board.finish(KEY, PR_URL, target="нет такого")

    assert result["pr_link_added"] is True
    assert result["done_set"] is False
    assert any("unsupported" in warning for warning in result["warnings"])
    assert state.status == "в работе"
    board.close()


def test_failed_description_update_does_not_block_the_status_change() -> None:
    state = Board(description_error=403)
    board = _board(state)

    result = board.finish(KEY, PR_URL, target="готово")

    assert result["pr_link_added"] is False
    assert result["done_set"] is True
    assert any("permission" in warning for warning in result["warnings"])
    assert TOKEN not in repr(result)
    board.close()


def test_mark_done_false_only_appends_the_pr_link() -> None:
    state = Board()
    board = _board(state)

    result = board.finish(KEY, PR_URL, mark_done=False, target="готово")

    assert result["pr_link_added"] is True
    assert result["done_set"] is False
    assert result["already_closed"] is False
    assert state.status == "в работе"
    board.close()


def test_task_already_at_the_target_with_the_link_reports_already_closed() -> None:
    state = Board(description=f"## Задача\n\nтело\n\nPR: {PR_URL}", status="готово")
    board = _board(state)

    result = board.finish(KEY, PR_URL, target="готово")

    assert result["pr_link_added"] is False
    assert result["done_set"] is False
    assert result["already_closed"] is True
    assert [call[0] for call in state.calls] == ["GET"]
    board.close()


def test_missing_task_is_reported_as_not_found() -> None:
    state = Board()
    board = _board(state)

    with pytest.raises(BoardProviderError) as exc_info:
        board.finish("PRI-999999999", PR_URL, target="готово")

    assert exc_info.value.category == "not_found"
    board.close()


def test_finish_resolves_a_custom_task_id_through_the_team_id() -> None:
    calls: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, dict(request.url.params)))
        if request.method == "GET" and request.url.path == "/api/v2/task/PRI-42":
            return httpx.Response(
                200,
                json={
                    "id": "4cd",
                    "custom_id": "PRI-42",
                    "markdown_description": "тело",
                    "status": {"status": "в работе", "type": "custom"},
                },
            )
        if request.method == "PUT" and request.url.path == "/api/v2/task/4cd":
            return httpx.Response(200, json={"id": "4cd"})
        return httpx.Response(404, json={"err": "not found"})

    board = ClickUpBoard(
        token=TOKEN,
        list_id="901",
        key_prefix="PRI",
        team_id="9007",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    )
    result = board.finish("PRI-42", PR_URL, target="готово")

    custom_calls = [call for call in calls if call[1] == "/api/v2/task/PRI-42"]

    assert result["board_id"] == "4cd"
    assert result["pr_link_added"] is True
    assert result["done_set"] is True
    assert custom_calls[0][2]["custom_task_ids"] == "true"
    assert custom_calls[0][2]["team_id"] == "9007"
    board.close()
