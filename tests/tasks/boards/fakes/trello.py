"""Фейк доски Trello для общего contract-набора.

Моделирует существенные для адаптера свойства Trello: auth query-параметрами
``key``/``token``, листинг карточек страницами с курсором ``before`` по id карточки,
списки доски в роли статуса, чеклисты и вложения отдельными эндпоинтами.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from reviewer.tasks.boards.trello import TrelloBoard
from tests.tasks.boards.fakes.base import (
    FakeState,
    ProviderAdapter,
    RecordingTransport,
    record,
    request_json,
)

SECRET = "trello-contract-token"
API_KEY = "trello-contract-app-key"
API_BASE = "https://api.trello.com/1"
BOARD_ID = "5f00000000000000000000b0"
KEY_PREFIX = "TRL"
PAGE = 3
CARDS = 7
BACKLOG = "5f00000000000000000000l1"
DONE = "5f00000000000000000000l2"
LISTS = [{"id": BACKLOG, "name": "Backlog"}, {"id": DONE, "name": "Done"}]
OAUTH_HEADER = f'OAuth oauth_consumer_key="{API_KEY}", oauth_token="{SECRET}"'


@dataclass
class State(FakeState):
    """Состояние фейка Trello: описания и списки карточек плюс созданные карточки."""

    descs: dict[int, str] = field(default_factory=dict)
    lists: dict[int, str] = field(default_factory=dict)
    created: list[dict] = field(default_factory=list)


def _card_id(number: int) -> str:
    """id карточки монотонен по времени создания — на этом держится курсор ``before``."""
    return f"{number:024x}"


def _card(state: State, number: int) -> dict[str, Any]:
    """Карточка: активность обратна номеру, поэтому свежайшая — карточка №1."""
    return {
        "id": _card_id(number),
        "idShort": number,
        "name": f"Задача {number}",
        "desc": state.descs.get(number, f"## Проблема\n\nОписание TRL-{number}"),
        "dateLastActivity": f"2026-07-23T09:{(60 - number) % 60:02d}:00.000Z",
        "idList": state.lists.get(number, BACKLOG),
        "idBoard": BOARD_ID,
        "shortLink": f"sh{number:06d}",
        "shortUrl": f"https://trello.com/c/sh{number:06d}",
        "closed": False,
    }


def _page(state: State, params: httpx.QueryParams) -> list[dict]:
    """Страница листинга: карточки от новых к старым, фильтр ``before`` по id."""
    limit = int(params.get("limit") or 1000)
    before = params.get("before")
    rows = [_card(state, number) for number in range(CARDS, 0, -1)]
    if before:
        rows = [row for row in rows if row["id"] < before]
    return rows[:limit]


def _number_from_card_id(card_id: str) -> int:
    try:
        return int(card_id, 16)
    except ValueError:
        return 0


def _trello_handler(
    state: State,
    *,
    error_status: int | None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        record(state, request)
        if error_status is not None:
            return httpx.Response(error_status, json={"token": SECRET})
        path = request.url.path
        method = request.method
        if method == "GET" and path == "/1/members/me":
            return httpx.Response(
                200, json={"id": "member-1", "username": "robot", "fullName": "Робот"}
            )
        if method == "GET" and path == f"/1/boards/{BOARD_ID}":
            return httpx.Response(200, json={"id": BOARD_ID, "name": "Доска TRL", "closed": False})
        if method == "GET" and path == f"/1/boards/{BOARD_ID}/lists":
            return httpx.Response(200, json=LISTS)
        if method == "GET" and path == f"/1/boards/{BOARD_ID}/cards":
            return httpx.Response(200, json=_page(state, request.url.params))
        if method == "GET" and path.startswith(f"/1/boards/{BOARD_ID}/cards/"):
            short = path.rsplit("/", 1)[-1]
            if short.isdigit() and 1 <= int(short) <= len(state.created) + CARDS:
                return httpx.Response(200, json=_card(state, int(short)))
            return httpx.Response(404, json={})
        if method == "GET" and path.endswith("/checklists"):
            number = _number_from_card_id(path.split("/")[3])
            if number != 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "chk-1",
                        "name": "Критерии приёмки",
                        "checkItems": [
                            {"id": "ci-1", "name": "Тесты проходят", "state": "incomplete"}
                        ],
                    }
                ],
            )
        if method == "GET" and path.endswith("/attachments"):
            number = _number_from_card_id(path.split("/")[3])
            if number != 1:
                return httpx.Response(200, json=[])
            card_id = _card_id(number)
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "att-1",
                        "name": "spec.txt",
                        "url": (
                            f"https://trello.com/1/cards/{card_id}"
                            "/attachments/att-1/download/spec.txt"
                        ),
                        "mimeType": "text/plain",
                        "bytes": 24,
                        "isUpload": True,
                    }
                ],
            )
        if method == "GET" and path.endswith("/download/spec.txt"):
            if request.headers.get("Authorization") != OAUTH_HEADER:
                return httpx.Response(401, json={"token": SECRET})
            return httpx.Response(200, text="Критерий из вложения")
        if method == "POST" and path == "/1/cards":
            payload = request_json(request)
            number = CARDS + len(state.created) + 1
            state.created.append(payload)
            state.descs[number] = str(payload.get("desc") or "")
            state.lists[number] = str(payload.get("idList") or BACKLOG)
            return httpx.Response(200, json=_card(state, number))
        if method == "PUT" and path.startswith("/1/cards/"):
            number = _number_from_card_id(path.rsplit("/", 1)[-1])
            payload = request_json(request)
            if "desc" in payload:
                state.descs[number] = str(payload["desc"])
            if "idList" in payload:
                state.lists[number] = str(payload["idList"])
            return httpx.Response(200, json=_card(state, number))
        return httpx.Response(404, json={})

    return handle


def build(
    *,
    state: State | None = None,
    forbidden: bool = False,
    error_status: int | None = None,
) -> tuple[TrelloBoard, State]:
    """Собрать TrelloBoard с транспортом через конструктор (без сети)."""
    state = state or State()
    status = error_status or (403 if forbidden else None)
    provider = TrelloBoard(
        api_key=API_KEY,
        api_token=SECRET,
        api_base=API_BASE,
        board_id=BOARD_ID,
        key_prefix=KEY_PREFIX,
        key_pattern=r"TRL-\d+",
        url_template="https://trello.com/c/{code}",
        attachment_max_bytes=1000,
        attachment_timeout=1.0,
        attachment_store_chars=1000,
        page_size=PAGE,
        transport=RecordingTransport(_trello_handler(state, error_status=status), state),
        sleeper=lambda _: None,
    )
    return provider, state


ADAPTER = ProviderAdapter(
    board_type="trello",
    secret=SECRET,
    project=KEY_PREFIX,
    key="TRL-1",
    finish_key="TRL-2",
    target_id=DONE,
    target_label="Done",
    missing_target="Missing",
    factory=build,
    min_rows=PAGE,
    page_paths=("/cards",),
)
