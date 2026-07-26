"""Создание задачи Weeek: locations, HTML-описание, резолв колонки и fallback."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.weeek import WeeekBoard

TOKEN = "weeek-secret-token"
COLUMNS = {
    "success": True,
    "boardColumns": [
        {"id": 8, "name": "Backlog", "boardId": 6},
        {"id": 9, "name": "Done", "boardId": 6},
    ],
}
DOC_MD = "# Заголовок\n\nТекст задачи.\n"


def _board(handler, **kwargs) -> WeeekBoard:
    params: dict[str, Any] = {
        "api_token": TOKEN,
        "project_id": "4",
        "board_id": "6",
        "key_prefix": "WEEEK",
        "url_template": "https://app.weeek.net/ws/1/task/{code}",
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _seconds: None,
    }
    params.update(kwargs)
    return WeeekBoard(**params)


def _handler(bodies: list[dict[str, Any]], *, created: dict[str, Any] | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tm/board-columns"):
            return httpx.Response(200, json=COLUMNS)
        if request.method == "POST" and path.endswith("/tm/tasks"):
            import json

            bodies.append(json.loads(request.content.decode()))
            task = created if created is not None else {"id": 77, "title": "Новая задача"}
            return httpx.Response(200, json={"success": True, "task": task})
        return httpx.Response(404, json={"success": False})

    return handle


def test_create_posts_title_locations_and_html_description() -> None:
    bodies: list[dict[str, Any]] = []

    result = _board(_handler(bodies)).create(
        DOC_MD,
        title="Новая задача",
        target="Done",
        project="WEEEK",
    )

    assert bodies == [
        {
            "title": "Новая задача",
            "description": "<h2>Заголовок</h2><p>Текст задачи.</p>",
            "locations": [{"projectId": 4, "boardColumnId": 9}],
        }
    ]
    assert result == {
        "key": "WEEEK-77",
        "url": "https://app.weeek.net/ws/1/task/WEEEK-77",
        "board_id": "77",
        "target_resolved": "Done",
        "warnings": [],
    }


def test_create_resolves_the_target_by_column_id() -> None:
    bodies: list[dict[str, Any]] = []

    result = _board(_handler(bodies)).create(
        DOC_MD,
        title="Новая задача",
        target="9",
        project=None,
    )

    assert bodies[0]["locations"] == [{"projectId": 4, "boardColumnId": 9}]
    assert result["target_resolved"] == "Done"
    assert result["warnings"] == []


def test_unknown_target_falls_back_to_the_first_column_with_a_warning() -> None:
    bodies: list[dict[str, Any]] = []

    result = _board(_handler(bodies)).create(
        DOC_MD,
        title="Новая задача",
        target="Missing",
        project=None,
    )

    assert bodies[0]["locations"] == [{"projectId": 4, "boardColumnId": 8}]
    assert result["target_resolved"] == "Backlog"
    assert result["warnings"]


def test_numeric_project_argument_overrides_the_configured_option() -> None:
    bodies: list[dict[str, Any]] = []

    _board(_handler(bodies)).create(DOC_MD, title="Новая задача", target=None, project="12")

    assert bodies[0]["locations"] == [{"projectId": 12, "boardColumnId": 8}]


def test_create_without_a_board_still_files_the_task_and_warns() -> None:
    bodies: list[dict[str, Any]] = []

    result = _board(_handler(bodies), board_id="").create(
        DOC_MD,
        title="Новая задача",
        target=None,
        project=None,
    )

    assert bodies[0]["locations"] == [{"projectId": 4, "boardColumnId": None}]
    assert result["target_resolved"] is None
    assert result["warnings"]


def test_create_without_a_project_is_a_configuration_error() -> None:
    bodies: list[dict[str, Any]] = []

    with pytest.raises(BoardProviderError) as exc_info:
        _board(_handler(bodies), project_id="").create(
            DOC_MD,
            title="Новая задача",
            target=None,
            project="WEEEK",
        )

    assert exc_info.value.category == "configuration"
    assert bodies == []


def test_create_without_a_title_is_a_configuration_error() -> None:
    bodies: list[dict[str, Any]] = []

    with pytest.raises(BoardProviderError) as exc_info:
        _board(_handler(bodies)).create(DOC_MD, title="   ", target=None, project=None)

    assert exc_info.value.category == "configuration"
    assert bodies == []


def test_create_response_without_a_task_id_is_reported_as_unsupported() -> None:
    bodies: list[dict[str, Any]] = []

    with pytest.raises(BoardProviderError) as exc_info:
        _board(_handler(bodies, created={"title": "Новая задача"})).create(
            DOC_MD,
            title="Новая задача",
            target=None,
            project=None,
        )

    assert exc_info.value.category == "unsupported"
    assert TOKEN not in f"{exc_info.value!r}"
