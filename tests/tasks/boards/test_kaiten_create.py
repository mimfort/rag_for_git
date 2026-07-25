"""Создание карточки Kaiten: POST /cards, резолв колонки, fallback с warning."""
from __future__ import annotations

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from tests.tasks.boards.fakes.base import request_json
from tests.tasks.boards.test_kaiten_read import TOKEN, board, columns

DOC_MD = "## Проблема\n\nСломано\n\n## Критерии приёмки\n\n1. Починено"


def create_handler(bodies: list[dict] | None = None):
    """Транспорт создания: колонки доски + POST /cards, отдающий созданную карточку."""
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/latest/boards/1/columns":
            return httpx.Response(200, json=columns())
        if request.method == "POST" and request.url.path == "/api/latest/cards":
            body = request_json(request)
            if bodies is not None:
                bodies.append(body)
            return httpx.Response(
                200,
                json={
                    "id": 777,
                    "title": body.get("title"),
                    "description": body.get("description"),
                    "board_id": 1,
                    "column_id": body.get("column_id") or 1,
                    "updated": "2026-07-24T12:30:45.500Z",
                    "state": 1,
                },
            )
        return httpx.Response(404, json={})

    return handle


def test_create_sends_markdown_as_is_with_board_and_column() -> None:
    bodies: list[dict] = []

    result = board(create_handler(bodies)).create(
        DOC_MD, title="Новая карточка", target="3", project="KTN"
    )

    assert bodies == [{
        "title": "Новая карточка",
        "board_id": 1,
        "description": DOC_MD,
        "column_id": 3,
    }]
    assert result == {
        "key": "KTN-777",
        "url": "https://acme.kaiten.ru/777",
        "board_id": "777",
        "target_resolved": "Готово",
        "warnings": [],
    }


def test_create_resolves_target_by_exact_label() -> None:
    bodies: list[dict] = []

    result = board(create_handler(bodies)).create(
        DOC_MD, title="Новая карточка", target="Готово", project="KTN"
    )

    assert bodies[0]["column_id"] == 3
    assert result["target_resolved"] == "Готово"
    assert result["warnings"] == []


def test_unknown_target_falls_back_to_the_board_default_with_a_warning() -> None:
    bodies: list[dict] = []

    result = board(create_handler(bodies)).create(
        DOC_MD, title="Новая карточка", target="Missing", project="KTN"
    )

    assert "column_id" not in bodies[0]
    assert result["key"] == "KTN-777"
    assert result["target_resolved"] == "Очередь"
    assert result["target_resolved"] != "Missing"
    assert result["warnings"] and "Missing" in result["warnings"][0]


def test_create_without_target_reports_the_actual_column() -> None:
    result = board(create_handler()).create(
        DOC_MD, title="Новая карточка", target=None, project="KTN"
    )

    assert result["target_resolved"] == "Очередь"
    assert result["warnings"] == []


def test_create_uses_the_url_template_when_configured() -> None:
    provider = board(
        create_handler(),
        url_template="https://acme.kaiten.ru/space/7/card/{code}",
    )

    result = provider.create(DOC_MD, title="Новая карточка", target="3", project="KTN")

    assert result["url"] == "https://acme.kaiten.ru/space/7/card/777"


def test_create_requires_a_board_id() -> None:
    provider = board(create_handler(), board_id="")

    with pytest.raises(BoardProviderError) as error:
        provider.create(DOC_MD, title="Новая карточка", target=None, project="KTN")

    assert error.value.category == "configuration"
    assert TOKEN not in repr(error.value)


def test_non_numeric_board_option_is_a_configuration_error() -> None:
    provider = board(create_handler(), board_id="acme-board")

    with pytest.raises(BoardProviderError) as error:
        provider.create(DOC_MD, title="Новая карточка", target=None, project="KTN")

    assert error.value.category == "configuration"


def test_create_without_a_card_id_in_the_response_is_an_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/latest/boards/1/columns":
            return httpx.Response(200, json=columns())
        return httpx.Response(200, json={"title": "Новая карточка"})

    with pytest.raises(BoardProviderError) as error:
        board(handle).create(DOC_MD, title="Новая карточка", target=None, project="KTN")

    assert error.value.category == "unsupported"


def test_create_is_not_retried_and_keeps_the_secret_out_of_errors() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/latest/boards/1/columns":
            return httpx.Response(200, json=columns())
        calls.append(request.method)
        return httpx.Response(500, json={"token": TOKEN})

    with pytest.raises(BoardProviderError) as error:
        board(handle).create(DOC_MD, title="Новая карточка", target="3", project="KTN")

    assert calls == ["POST"]
    assert error.value.category == "transient"
    assert TOKEN not in f"{error.value!s}{error.value!r}"
