"""Создание карточки Trello: резолв списка, тело запроса, fallback с warning."""
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
CARD = "5f00000000000000000000c8"
DOC = "## Проблема\n\nНужен адаптер Trello"

LISTS = [
    {"id": BACKLOG, "name": "Backlog"},
    {"id": DONE, "name": "Done"},
]


def _board(handler, **kwargs) -> TrelloBoard:
    return TrelloBoard(
        api_key=API_KEY,
        api_token=TOKEN,
        board_id=BOARD,
        key_prefix="TRL",
        url_template="https://trello.test/task/{code}",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
        **kwargs,
    )


def _handler(requests: list[httpx.Request], *, lists=None, created=None):
    body = created if created is not None else {
        "id": CARD,
        "idShort": 8,
        "shortLink": "sh000008",
        "shortUrl": "https://trello.com/c/sh000008",
        "idList": BACKLOG,
    }

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/lists"):
            return httpx.Response(200, json=LISTS if lists is None else lists)
        if request.method == "POST" and request.url.path == "/1/cards":
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={})

    return handle


def test_card_is_created_in_the_target_list_with_markdown_description() -> None:
    requests: list[httpx.Request] = []
    result = _board(_handler(requests)).create(
        DOC, title="Адаптер Trello", target=DONE, project="TRL"
    )

    assert result == {
        "key": "TRL-8",
        "url": "https://trello.com/c/sh000008",
        "board_id": CARD,
        "target_resolved": "Done",
        "warnings": [],
    }
    post = requests[-1]
    assert (post.method, post.url.path) == ("POST", "/1/cards")
    assert json.loads(post.content) == {
        "idList": DONE,
        "name": "Адаптер Trello",
        "desc": DOC,
        "pos": "bottom",
    }
    assert dict(post.url.params) == {"key": API_KEY, "token": TOKEN}


def test_target_is_resolved_by_unique_list_name() -> None:
    requests: list[httpx.Request] = []
    result = _board(_handler(requests)).create(
        DOC, title="Адаптер Trello", target="Done", project="TRL"
    )

    assert result["target_resolved"] == "Done"
    assert result["warnings"] == []
    assert json.loads(requests[-1].content)["idList"] == DONE


def test_missing_target_falls_back_to_the_first_list_with_a_warning() -> None:
    requests: list[httpx.Request] = []
    result = _board(_handler(requests)).create(
        DOC, title="Адаптер Trello", target="Нет такого", project="TRL"
    )

    assert result["target_resolved"] == "Backlog"
    assert result["warnings"] and "не найден" in result["warnings"][0]
    assert json.loads(requests[-1].content)["idList"] == BACKLOG


def test_ambiguous_list_name_falls_back_to_the_first_list_with_a_warning() -> None:
    requests: list[httpx.Request] = []
    twins = [
        {"id": BACKLOG, "name": "Готово"},
        {"id": DONE, "name": "Готово"},
    ]
    result = _board(_handler(requests, lists=twins)).create(
        DOC, title="Адаптер Trello", target="Готово", project="TRL"
    )

    assert result["target_resolved"] == "Готово"
    assert result["warnings"] and "неоднозначен" in result["warnings"][0]
    assert json.loads(requests[-1].content)["idList"] == BACKLOG


def test_board_without_lists_is_a_configuration_error() -> None:
    requests: list[httpx.Request] = []
    with pytest.raises(BoardProviderError) as exc_info:
        _board(_handler(requests, lists=[])).create(
            DOC, title="Адаптер Trello", target=None, project="TRL"
        )

    assert exc_info.value.category == "configuration"
    assert [request.method for request in requests] == ["GET"]


def test_response_without_id_short_keeps_native_identifier_as_key() -> None:
    requests: list[httpx.Request] = []
    result = _board(
        _handler(requests, created={"id": CARD, "shortLink": "sh000008"})
    ).create(DOC, title="Адаптер Trello", target=None, project="TRL")

    assert result["key"] == "sh000008"
    assert result["url"] == "https://trello.test/task/sh000008"
    assert result["warnings"] and "idShort" in result["warnings"][0]


def test_create_without_card_id_in_response_raises() -> None:
    with pytest.raises(BoardProviderError) as exc_info:
        _board(_handler([], created={})).create(
            DOC, title="Адаптер Trello", target=None, project="TRL"
        )

    assert exc_info.value.category == "unsupported"
