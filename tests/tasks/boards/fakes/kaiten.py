"""Фейк доски Kaiten для общего contract-набора.

Listing отдаёт те же карточки, что и точечный ``GET /cards/{id}`` (включая инлайновые
чеклисты и файлы), чтобы contract-проверка «один маппер для iter_raw и fetch_one»
сравнивала одинаковые payload'ы. Состояние карточки завершения живёт в ``State``,
поэтому повторный ``finish`` видит уже дописанную PR-ссылку и уже нужную колонку.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from reviewer.tasks.boards.kaiten import KaitenBoard
from tests.tasks.boards.fakes.base import (
    FakeState,
    ProviderAdapter,
    RecordingTransport,
    record,
    request_json,
)

SECRET = "kaiten-contract-secret"
BASE_URL = "https://kaiten-contract.kaiten.ru"
BOARD_ID = "1"
PREFIX = "KTN"
CARDS = 150            # 2 страницы по limit=100 → минимум два страничных запроса
FINISH_CARD = 102


@dataclass
class State(FakeState):
    """Состояние фейка Kaiten: описание и колонка карточки завершения, счётчик созданий."""

    description: str = "## Задача\n\nСвязана с KTN-777."
    column_id: int = 2
    created: int = 0


def _columns() -> list[dict[str, Any]]:
    return [
        {"id": 1, "title": "Очередь", "type": 1, "sort_order": 1, "board_id": 1,
         "subcolumns": []},
        {"id": 2, "title": "В работе", "type": 2, "sort_order": 2, "board_id": 1,
         "subcolumns": []},
        {"id": 3, "title": "Готово", "type": 3, "sort_order": 3, "board_id": 1,
         "subcolumns": []},
    ]


def _card(number: int) -> dict[str, Any]:
    """Карточка Kaiten; у первой есть дочерняя карточка, чеклист и вложение spec.txt."""
    card_id = 100 + number
    payload: dict[str, Any] = {
        "id": card_id,
        "title": f"Карточка {number}",
        "description": f"## Карточка {number}\n\nОписание в markdown.",
        "created": "2026-07-01T10:00:00.000Z",
        "updated": f"2026-07-23T09:{number % 60:02d}:00.000Z",
        "board_id": 1,
        "column_id": 2,
        "lane_id": 5,
        "state": 2,
        "condition": 1,
        "children": [],
        "checklists": [],
        "files": [],
    }
    if number == 1:
        payload["children"] = [{"id": 909, "title": "Дочерняя карточка"}]
        payload["checklists"] = [
            {
                "id": 11,
                "name": "Критерии",
                "items": [
                    {"id": 1, "text": "Код смержен", "checked": True, "sort_order": 1},
                    {"id": 2, "text": "Тесты зелёные", "checked": False, "sort_order": 2},
                ],
            }
        ]
        payload["files"] = [
            {"id": 5, "name": "spec.txt", "url": f"{BASE_URL}/files/spec.txt", "size": 24,
             "type": 1},
        ]
    return payload


def _state_card(state: State) -> dict[str, Any]:
    """Карточка завершения из изменяемого состояния фейка."""
    payload = _card(FINISH_CARD - 100)
    payload["description"] = state.description
    payload["column_id"] = state.column_id
    return payload


def _handler(
    state: State,
    *,
    error_status: int | None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        record(state, request)
        if error_status is not None:
            return httpx.Response(error_status, json={"token": SECRET})
        path = request.url.path
        if request.method == "GET" and path == "/api/latest/users/current":
            return httpx.Response(200, json={"id": 9, "full_name": "Робот",
                                             "username": "robot", "company_id": 3})
        if request.method == "GET" and path == f"/api/latest/boards/{BOARD_ID}/columns":
            return httpx.Response(200, json=_columns())
        if request.method == "GET" and path == "/api/latest/cards":
            offset = int(request.url.params.get("offset", 0))
            limit = int(request.url.params.get("limit", 100))
            cards = [_card(number) for number in range(1, CARDS + 1)]
            return httpx.Response(200, json=cards[offset : offset + limit])
        if request.method == "POST" and path == "/api/latest/cards":
            body = request_json(request)
            state.created += 1
            return httpx.Response(200, json={
                "id": 777,
                "title": body.get("title"),
                "description": body.get("description"),
                "board_id": 1,
                "column_id": body.get("column_id") or 1,
                "updated": "2026-07-24T12:30:45.500Z",
                "state": 1,
            })
        if path == f"/api/latest/cards/{FINISH_CARD}":
            if request.method == "PATCH":
                body = request_json(request)
                if "description" in body:
                    state.description = str(body["description"])
                if "column_id" in body:
                    state.column_id = int(body["column_id"])
                return httpx.Response(200, json=_state_card(state))
            return httpx.Response(200, json=_state_card(state))
        if request.method == "GET" and path.startswith("/api/latest/cards/"):
            number = int(path.rsplit("/", 1)[-1]) - 100
            if 1 <= number <= CARDS:
                return httpx.Response(200, json=_card(number))
            return httpx.Response(404, json={})
        if request.method == "GET" and path == "/files/spec.txt":
            return httpx.Response(200, text="Критерий из вложения")
        return httpx.Response(404, json={})

    return handle


def build(
    *,
    state: State | None = None,
    forbidden: bool = False,
    error_status: int | None = None,
) -> tuple[KaitenBoard, State]:
    """Собрать KaitenBoard с транспортом через конструктор (без сети)."""
    state = state or State()
    status = error_status or (403 if forbidden else None)
    provider = KaitenBoard(
        token=SECRET,
        base_url=BASE_URL,
        board_id=BOARD_ID,
        key_prefix=PREFIX,
        key_pattern=r"KTN-\d+",
        attachment_max_bytes=1000,
        attachment_timeout=1.0,
        attachment_store_chars=1000,
        transport=RecordingTransport(_handler(state, error_status=status), state),
        sleeper=lambda _: None,
    )
    return provider, state


ADAPTER = ProviderAdapter(
    board_type="kaiten",
    secret=SECRET,
    project=PREFIX,
    key=f"{PREFIX}-101",
    finish_key=f"{PREFIX}-{FINISH_CARD}",
    target_id="3",
    target_label="Готово",
    missing_target="Missing",
    factory=build,
    min_rows=100,
    page_paths=("/cards",),
)
