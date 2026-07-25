"""Закрытие карточки Kaiten: PATCH /cards/{id} — перенос в done-колонку + PR-ссылка."""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from tests.tasks.boards.fakes.base import request_json
from tests.tasks.boards.test_kaiten_read import TOKEN, board, card, columns

PR_URL = "https://github.test/acme/widgets/pull/7"


@dataclass
class CardState:
    """Изменяемое состояние карточки: описание, колонка и записанные PATCH-тела."""

    description: str = "## Задача\n\nТекст"
    column_id: int = 2
    patches: list[dict] = field(default_factory=list)


def finish_handler(state: CardState, *, columns_payload: list[dict] | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/columns"):
            return httpx.Response(
                200, json=columns() if columns_payload is None else columns_payload
            )
        if path == "/api/latest/cards/102":
            if request.method == "PATCH":
                body = request_json(request)
                state.patches.append(body)
                if "description" in body:
                    state.description = body["description"]
                if "column_id" in body:
                    state.column_id = int(body["column_id"])
                return httpx.Response(200, json={"id": 102})
            return httpx.Response(200, json=card(
                2, description=state.description, column_id=state.column_id,
            ))
        return httpx.Response(404, json={})

    return handle


def test_first_finish_moves_the_card_and_appends_the_pr_link() -> None:
    state = CardState()

    result = board(finish_handler(state)).finish(
        "KTN-102", PR_URL, note="Проверено", target="3"
    )

    assert result["pr_link_added"] is True
    assert result["done_set"] is True
    assert result["already_closed"] is False
    assert result["key"] == "KTN-102"
    assert result["board_id"] == "102"
    assert result["warnings"] == []
    assert state.column_id == 3
    assert state.patches == [{"description": state.description, "column_id": 3}]
    assert PR_URL in state.description
    assert "Проверено" in state.description
    assert state.description.startswith("## Задача")


def test_finish_is_idempotent_on_a_repeated_call() -> None:
    state = CardState()
    provider = board(finish_handler(state))

    first = provider.finish("KTN-102", PR_URL, note="Проверено", target="3")
    second = provider.finish("KTN-102", PR_URL, note="Проверено", target="3")

    assert (first["pr_link_added"], first["done_set"]) == (True, True)
    assert (second["pr_link_added"], second["done_set"]) == (False, False)
    assert second["already_closed"] is True
    assert len(state.patches) == 1
    assert state.description.count(PR_URL) == 1


def test_existing_pr_link_without_the_marker_is_not_duplicated() -> None:
    state = CardState(description=f"Готово, см. {PR_URL}")

    result = board(finish_handler(state)).finish("KTN-102", PR_URL, target="3")

    assert result["pr_link_added"] is False
    assert result["done_set"] is True
    assert state.patches == [{"column_id": 3}]


def test_card_already_in_the_done_column_reports_no_move() -> None:
    state = CardState(description=f"{PR_URL}", column_id=3)

    result = board(finish_handler(state)).finish("KTN-102", PR_URL, target="3")

    assert result["done_set"] is False
    assert result["pr_link_added"] is False
    assert result["already_closed"] is True
    assert state.patches == []


def test_done_column_is_derived_from_the_numeric_type_without_a_target() -> None:
    state = CardState()

    result = board(finish_handler(state)).finish("KTN-102", PR_URL)

    assert result["done_set"] is True
    assert state.column_id == 3
    assert any("type" in warning for warning in result["warnings"])


def test_unknown_target_keeps_the_card_and_reports_a_warning() -> None:
    state = CardState()

    result = board(finish_handler(state)).finish("KTN-102", PR_URL, target="Missing")

    assert result["done_set"] is False
    assert result["pr_link_added"] is True
    assert result["already_closed"] is False
    assert state.column_id == 2
    assert result["warnings"] and "Missing" in result["warnings"][0]


def test_board_without_a_done_column_only_writes_the_pr_link() -> None:
    state = CardState()
    payload = [{"id": 1, "title": "Очередь", "type": 1, "sort_order": 1, "subcolumns": []}]

    result = board(finish_handler(state, columns_payload=payload)).finish("KTN-102", PR_URL)

    assert result["done_set"] is False
    assert result["pr_link_added"] is True
    assert state.patches == [{"description": state.description}]
    assert result["warnings"]


def test_mark_done_false_only_appends_the_pr_link() -> None:
    state = CardState()

    result = board(finish_handler(state)).finish("KTN-102", PR_URL, mark_done=False)

    assert result["done_set"] is False
    assert result["pr_link_added"] is True
    assert result["already_closed"] is False
    assert state.column_id == 2


def test_missing_card_is_reported_as_not_found_without_leaking_the_secret() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/columns"):
            return httpx.Response(200, json=columns())
        return httpx.Response(404, json={"token": TOKEN})

    with pytest.raises(BoardProviderError) as error:
        board(handle).finish("KTN-404", PR_URL, target="3")

    assert error.value.category == "not_found"
    assert TOKEN not in f"{error.value!s}{error.value!r}"


def test_broken_task_key_is_a_configuration_error() -> None:
    with pytest.raises(BoardProviderError) as error:
        board(finish_handler(CardState())).finish("KTN-без-номера", PR_URL, target="3")

    assert error.value.category == "configuration"
