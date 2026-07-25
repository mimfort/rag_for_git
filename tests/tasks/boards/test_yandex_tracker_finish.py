"""Закрытие задачи Yandex Tracker: PR-ссылка в описании + переход в done-цель.

Описание правится структурным ``PATCH /v3/issues/{key}`` с ``markupType: "md"``
(https://yandex.ru/support/tracker/en/api-ref/issues/patch-issue), статус меняется
переходом ``POST /v3/issues/{key}/transitions/{id}/_execute``. Текстовых команд нет —
значения уходят в JSON, поэтому фигурные скобки и пробелы в целях безопасны.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from reviewer.tasks.boards.yandex_tracker import YandexTrackerBoard

TOKEN = "yandex-tracker-secret-token"
PR_URL = "https://github.test/pull/7"

TRANSITIONS = [
    {"id": "close", "display": "Закрыть", "to": {"id": "3", "key": "closed", "display": "Закрыт"}}
]


@dataclass
class _Board:
    """Состояние фейковой задачи: описание и статус живут между вызовами finish."""

    description: str = "Описание TREK-2"
    status: dict = field(default_factory=lambda: {"id": "1", "key": "open", "display": "Открыт"})
    patches: list[dict] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)


def _handler(state: _Board, *, patch_status: int = 200, transitions_status: int = 200):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v3/issues/TREK-2":
            return httpx.Response(
                200,
                json={
                    "id": "60002",
                    "key": "TREK-2",
                    "summary": "Задача 2",
                    "description": state.description,
                    "status": state.status,
                    "queue": {"id": "3", "key": "TREK", "display": "Trek"},
                    "updatedAt": "2026-07-23T09:11:12.347+0000",
                },
            )
        if request.method == "PATCH" and path == "/v3/issues/TREK-2":
            if patch_status != 200:
                return httpx.Response(patch_status, json={"token": TOKEN})
            body = json.loads(request.content)
            state.patches.append(body)
            state.description = body["description"]
            return httpx.Response(200, json={"key": "TREK-2"})
        if request.method == "GET" and path.endswith("/transitions"):
            if transitions_status != 200:
                return httpx.Response(transitions_status, json={"token": TOKEN})
            return httpx.Response(200, json=TRANSITIONS)
        if request.method == "POST" and path.endswith("/_execute"):
            state.executed.append(path)
            state.status = {"id": "3", "key": "closed", "display": "Закрыт"}
            return httpx.Response(200, json=[{"status": state.status}])
        return httpx.Response(404, json={})

    return handle


def _board(state: _Board, **kwargs) -> YandexTrackerBoard:
    options: dict = {
        "token": TOKEN,
        "org_id": "org-42",
        "transport": httpx.MockTransport(_handler(state, **kwargs)),
        "sleeper": lambda _: None,
    }
    return YandexTrackerBoard(**options)


def test_first_finish_appends_the_pr_link_and_executes_the_transition() -> None:
    state = _Board()
    board = _board(state)

    result = board.finish("TREK-2", PR_URL, note="Проверено", target="closed")

    assert result["pr_link_added"] is True
    assert result["done_set"] is True
    assert result["already_closed"] is False
    assert result["board_id"] == "60002"
    assert result["warnings"] == []
    assert state.patches == [
        {
            "description": f"Описание TREK-2\n\nPR: {PR_URL}\n\nПроверено",
            "markupType": "md",
        }
    ]
    assert state.executed == ["/v3/issues/TREK-2/transitions/close/_execute"]
    board.close()


def test_second_finish_is_idempotent() -> None:
    state = _Board()
    board = _board(state)

    board.finish("TREK-2", PR_URL, note="Проверено", target="closed")
    second = board.finish("TREK-2", PR_URL, note="Проверено", target="closed")

    assert second["pr_link_added"] is False
    assert second["done_set"] is False
    assert second["already_closed"] is True
    assert len(state.patches) == 1
    assert len(state.executed) == 1
    board.close()


def test_done_target_is_matched_by_display_label_too() -> None:
    state = _Board()
    board = _board(state)

    first = board.finish("TREK-2", PR_URL, target="Закрыт")
    second = board.finish("TREK-2", PR_URL, target="Закрыт")

    assert first["done_set"] is True
    assert second["done_set"] is False
    assert second["already_closed"] is True
    board.close()


def test_empty_description_gets_only_the_pr_block() -> None:
    state = _Board(description="")
    board = _board(state)

    board.finish("TREK-2", PR_URL, target="closed")

    assert state.patches[0]["description"] == f"PR: {PR_URL}"
    board.close()


def test_mark_done_false_keeps_the_status() -> None:
    state = _Board()
    board = _board(state)

    result = board.finish("TREK-2", PR_URL, mark_done=False, target="closed")

    assert result["pr_link_added"] is True
    assert result["done_set"] is False
    assert state.executed == []
    board.close()


def test_missing_target_is_not_guessed() -> None:
    state = _Board()
    board = _board(state)

    result = board.finish("TREK-2", PR_URL, target=None)

    assert result["done_set"] is False
    assert result["already_closed"] is False
    assert result["warnings"]
    assert state.executed == []
    board.close()


def test_failed_description_patch_still_moves_the_status() -> None:
    state = _Board()
    board = _board(state, patch_status=403)

    result = board.finish("TREK-2", PR_URL, target="closed")

    assert result["pr_link_added"] is False
    assert result["done_set"] is True
    assert result["already_closed"] is False
    assert result["warnings"]
    assert TOKEN not in repr(result)
    board.close()


def test_unavailable_transitions_keep_the_pr_link() -> None:
    state = _Board()
    board = _board(state, transitions_status=403)

    result = board.finish("TREK-2", PR_URL, target="closed")

    assert result["pr_link_added"] is True
    assert result["done_set"] is False
    assert result["warnings"]
    board.close()


def test_status_already_at_target_only_adds_the_pr_link() -> None:
    state = _Board(status={"id": "3", "key": "closed", "display": "Закрыт"})
    board = _board(state)

    result = board.finish("TREK-2", PR_URL, target="closed")

    assert result["pr_link_added"] is True
    assert result["done_set"] is False
    assert result["already_closed"] is False
    assert state.executed == []
    board.close()


def test_finish_result_shape() -> None:
    state = _Board()
    board = _board(state)

    result = board.finish("TREK-2", PR_URL, target="closed")

    assert set(result) == {
        "key",
        "board_id",
        "done_set",
        "pr_link_added",
        "already_closed",
        "warnings",
    }
    board.close()
