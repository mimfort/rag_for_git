"""Закрытие задачи Trello: перенос в done-список + идемпотентная PR-ссылка в desc."""
from __future__ import annotations

import json

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.trello import TrelloBoard

API_KEY = "trello-app-key"
TOKEN = "trello-secret-token"
BOARD = "5f00000000000000000000b0"
BACKLOG = "5f00000000000000000000l1"
DONE = "5f00000000000000000000l2"
CARD = "5f00000000000000000000c2"
PR = "https://github.test/pull/7"

LISTS = [
    {"id": BACKLOG, "name": "Backlog"},
    {"id": DONE, "name": "Done"},
]


def _board(handler) -> TrelloBoard:
    return TrelloBoard(
        api_key=API_KEY,
        api_token=TOKEN,
        board_id=BOARD,
        key_prefix="TRL",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    )


def _handler(requests: list[httpx.Request], *, put_status: int = 200, id_list: str = BACKLOG):
    card = {
        "id": CARD,
        "idShort": 2,
        "name": "Задача 2",
        "desc": "## Проблема\n\nОписание",
        "dateLastActivity": "2026-07-23T09:00:00.000Z",
        "idList": id_list,
        "idBoard": BOARD,
        "shortLink": "sh000002",
        "shortUrl": "https://trello.com/c/sh000002",
        "closed": False,
    }

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/lists"):
            return httpx.Response(200, json=LISTS)
        if request.method == "GET" and path == f"/1/boards/{BOARD}/cards/2":
            return httpx.Response(200, json=card)
        if request.method == "GET" and path == f"/1/boards/{BOARD}/cards/404":
            return httpx.Response(404, json={"token": TOKEN})
        if request.method == "PUT" and path == f"/1/cards/{CARD}":
            if put_status != 200:
                return httpx.Response(put_status, json={"token": TOKEN})
            card.update(json.loads(request.content))
            return httpx.Response(200, json=card)
        return httpx.Response(404, json={})

    return handle, card


def test_finish_moves_the_card_and_adds_the_pr_link_then_is_idempotent() -> None:
    requests: list[httpx.Request] = []
    handler, card = _handler(requests)
    board = _board(handler)

    first = board.finish("TRL-2", PR, note="Проверено", target=DONE)
    puts = [request for request in requests if request.method == "PUT"]
    second = board.finish("TRL-2", PR, note="Проверено", target=DONE)

    assert first == {
        "key": "TRL-2",
        "board_id": CARD,
        "done_set": True,
        "pr_link_added": True,
        "already_closed": False,
        "warnings": [],
    }
    assert len(puts) == 1, "одна запись двигает dateLastActivity ровно один раз"
    assert json.loads(puts[0].content) == {
        "desc": f"## Проблема\n\nОписание\n\nPR: {PR}\nПроверено",
        "idList": DONE,
    }
    assert card["idList"] == DONE
    assert second["pr_link_added"] is False
    assert second["done_set"] is False
    assert second["already_closed"] is True
    assert len([request for request in requests if request.method == "PUT"]) == 1


def test_card_already_in_the_done_list_only_receives_the_pr_link() -> None:
    requests: list[httpx.Request] = []
    handler, _ = _handler(requests, id_list=DONE)

    result = _board(handler).finish("TRL-2", PR, target=DONE)

    assert result["pr_link_added"] is True
    assert result["done_set"] is False
    assert result["already_closed"] is False
    assert result["warnings"] == []
    assert json.loads(
        [request for request in requests if request.method == "PUT"][0].content
    ) == {"desc": f"## Проблема\n\nОписание\n\nPR: {PR}"}


def test_finish_without_target_never_guesses_the_done_list() -> None:
    requests: list[httpx.Request] = []
    handler, _ = _handler(requests)

    result = _board(handler).finish("TRL-2", PR, target=None)

    assert result["done_set"] is False
    assert result["pr_link_added"] is True
    assert result["warnings"] and "done-цель" in result["warnings"][0]
    assert not any(request.url.path.endswith("/lists") for request in requests)


def test_unknown_done_target_is_reported_without_moving_the_card() -> None:
    requests: list[httpx.Request] = []
    handler, card = _handler(requests)

    result = _board(handler).finish("TRL-2", PR, target="Нет такого")

    assert result["done_set"] is False
    assert result["pr_link_added"] is True
    assert result["already_closed"] is False
    assert result["warnings"] and "не перенесена" in result["warnings"][0]
    assert card["idList"] == BACKLOG


def test_failed_write_reports_no_change_with_a_safe_warning() -> None:
    requests: list[httpx.Request] = []
    handler, card = _handler(requests, put_status=403)

    result = _board(handler).finish("TRL-2", PR, target=DONE)

    assert result["pr_link_added"] is False
    assert result["done_set"] is False
    assert result["already_closed"] is False
    assert result["warnings"] and TOKEN not in repr(result)
    assert card["idList"] == BACKLOG


def test_unknown_task_key_is_not_found() -> None:
    requests: list[httpx.Request] = []
    handler, _ = _handler(requests)

    with pytest.raises(BoardProviderError) as exc_info:
        _board(handler).finish("TRL-404", PR, target=DONE)

    assert exc_info.value.category == "not_found"
    assert TOKEN not in repr(exc_info.value)
