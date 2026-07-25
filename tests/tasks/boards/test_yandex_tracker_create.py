"""Создание задачи Yandex Tracker: ``POST /v3/issues`` + перевод в запрошенный статус.

Обязательные поля создания — ``queue`` и ``summary``; markdown-описание передаётся
с ``markupType: "md"`` (https://yandex.ru/support/tracker/en/api-ref/issues/create-issue).
Статус на создании не задаётся напрямую: задача попадает в начальный статус workflow,
а запрошенная цель применяется переходом сразу после создания.
"""
from __future__ import annotations

import json

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.yandex_tracker import YandexTrackerBoard

TOKEN = "yandex-tracker-secret-token"
DOC = "# Новая задача\n\nКонтекст."

TRANSITIONS = [
    {
        "id": "close",
        "display": "Закрыть",
        "to": {"id": "3", "key": "closed", "display": "Закрыт"},
    }
]


def _board(handler, **kwargs) -> YandexTrackerBoard:
    options: dict = {
        "token": TOKEN,
        "org_id": "org-42",
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _: None,
    }
    options.update(kwargs)
    return YandexTrackerBoard(**options)


def _handler(
    seen: list[httpx.Request] | None = None,
    *,
    transitions_status: int = 200,
    execute_status: int = 200,
    created: dict | None = None,
):
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/v3/issues":
            return httpx.Response(
                201,
                json=created
                if created is not None
                else {
                    "id": "60077",
                    "key": "TREK-77",
                    "status": {"id": "1", "key": "open", "display": "Открыт"},
                },
            )
        if request.method == "GET" and path.endswith("/transitions"):
            if transitions_status != 200:
                return httpx.Response(transitions_status, json={"token": TOKEN})
            return httpx.Response(200, json=TRANSITIONS)
        if request.method == "POST" and path.endswith("/_execute"):
            if execute_status != 200:
                return httpx.Response(execute_status, json={"token": TOKEN})
            return httpx.Response(200, json=[{"status": {"key": "closed"}}])
        return httpx.Response(404, json={})

    return handle


def test_create_sends_queue_summary_and_markdown_description() -> None:
    seen: list[httpx.Request] = []
    board = _board(_handler(seen))

    result = board.create(DOC, title="Новая задача", target=None, project="TREK")

    created = next(
        request for request in seen if request.method == "POST" and request.url.path == "/v3/issues"
    )
    assert json.loads(created.content) == {
        "queue": {"key": "TREK"},
        "summary": "Новая задача",
        "description": DOC,
        "markupType": "md",
    }
    assert result["key"] == "TREK-77"
    assert result["board_id"] == "60077"
    assert result["url"] == "https://tracker.yandex.ru/TREK-77"
    assert result["target_resolved"] == "open"
    assert result["warnings"] == []
    board.close()


def test_target_status_is_applied_with_a_transition() -> None:
    seen: list[httpx.Request] = []
    board = _board(_handler(seen))

    result = board.create(DOC, title="Новая задача", target="closed", project="TREK")

    assert result["target_resolved"] == "closed"
    assert result["warnings"] == []
    assert [
        (request.method, request.url.path)
        for request in seen
        if "transitions" in request.url.path
    ] == [
        ("GET", "/v3/issues/TREK-77/transitions"),
        ("POST", "/v3/issues/TREK-77/transitions/close/_execute"),
    ]
    board.close()


def test_target_is_resolved_by_display_label_into_the_canonical_key() -> None:
    board = _board(_handler())

    result = board.create(DOC, title="Новая задача", target="Закрыт", project="TREK")

    assert result["target_resolved"] == "closed"
    assert result["warnings"] == []
    board.close()


def test_unknown_target_falls_back_to_the_created_status_with_a_warning() -> None:
    board = _board(_handler())

    result = board.create(DOC, title="Новая задача", target="Missing", project="TREK")

    assert result["key"] == "TREK-77"
    assert result["target_resolved"] == "open"
    assert result["target_resolved"] != "Missing"
    assert result["warnings"]
    board.close()


def test_unavailable_transitions_do_not_break_creation() -> None:
    board = _board(_handler(transitions_status=403))

    result = board.create(DOC, title="Новая задача", target="closed", project="TREK")

    assert result["key"] == "TREK-77"
    assert result["target_resolved"] == "open"
    assert result["warnings"]
    assert TOKEN not in repr(result)
    board.close()


def test_failed_transition_execution_is_reported_as_a_warning() -> None:
    board = _board(_handler(execute_status=409))

    result = board.create(DOC, title="Новая задача", target="closed", project="TREK")

    assert result["key"] == "TREK-77"
    assert result["target_resolved"] == "open"
    assert result["warnings"]
    board.close()


def test_queue_option_is_used_when_project_is_absent() -> None:
    seen: list[httpx.Request] = []
    board = _board(_handler(seen), queue="TREK")

    board.create(DOC, title="Новая задача", target=None, project=None)

    created = next(request for request in seen if request.url.path == "/v3/issues")
    assert json.loads(created.content)["queue"] == {"key": "TREK"}
    board.close()


def test_missing_queue_is_a_configuration_error() -> None:
    board = _board(_handler())

    with pytest.raises(BoardProviderError) as exc_info:
        board.create(DOC, title="Новая задача", target=None, project=None)

    assert exc_info.value.category == "configuration"
    board.close()


def test_response_without_a_key_is_an_error() -> None:
    board = _board(_handler(created={"id": "60077"}))

    with pytest.raises(BoardProviderError) as exc_info:
        board.create(DOC, title="Новая задача", target=None, project="TREK")

    assert exc_info.value.category == "unsupported"
    board.close()
