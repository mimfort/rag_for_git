"""Создание задачи ClickUp: markdown_content, резолв статуса, fallback с warning."""
from __future__ import annotations

import json

import httpx
import pytest

from reviewer.tasks.boards.clickup import ClickUpBoard
from reviewer.tasks.boards.errors import BoardProviderError

TOKEN = "pk_clickup_create_secret"
DEFAULT_STATUS = "к выполнению"
LIST_PAYLOAD = {
    "id": "901",
    "statuses": [
        {"status": DEFAULT_STATUS, "orderindex": 0, "type": "open"},
        {"status": "готово", "orderindex": 1, "type": "closed"},
    ],
}
DOC = "## Проблема\n\nНужен адаптер"


def _board(handler, **kwargs) -> ClickUpBoard:
    params: dict = {
        "token": TOKEN,
        "list_id": "901",
        "key_prefix": "PRI",
        "key_pattern": r"PRI-\d+",
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _: None,
    }
    params.update(kwargs)
    return ClickUpBoard(**params)


def _handler(bodies: list[dict], *, list_status: int = 200, created: dict | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v2/list/901":
            if list_status != 200:
                return httpx.Response(list_status, json={"token": TOKEN})
            return httpx.Response(200, json=LIST_PAYLOAD)
        if request.method == "POST" and request.url.path == "/api/v2/list/901/task":
            body = json.loads(request.content.decode())
            bodies.append(body)
            if created is not None:
                return httpx.Response(200, json=created)
            return httpx.Response(
                200,
                json={
                    "id": "5ef",
                    "name": body["name"],
                    "status": {"status": body.get("status") or DEFAULT_STATUS, "type": "open"},
                    "url": "https://app.clickup.com/t/5ef",
                    "date_updated": "1784797200000",
                },
            )
        return httpx.Response(404, json={"err": "not found"})

    return handle


def test_create_sends_markdown_content_and_reports_the_requested_status() -> None:
    bodies: list[dict] = []
    board = _board(_handler(bodies))

    result = board.create(DOC, title="Новая задача", target="готово", project="PRI")

    assert bodies == [{"name": "Новая задача", "markdown_content": DOC, "status": "готово"}]
    assert result["key"] == f"PRI-{int('5ef', 36)}"
    assert result["board_id"] == "5ef"
    assert result["url"] == "https://app.clickup.com/t/5ef"
    assert result["target_resolved"] == "готово"
    assert result["warnings"] == []
    board.close()


def test_create_matches_the_target_case_insensitively() -> None:
    bodies: list[dict] = []
    board = _board(_handler(bodies))

    result = board.create(DOC, title="Новая задача", target="ГОТОВО", project="PRI")

    assert bodies[0]["status"] == "готово"
    assert result["target_resolved"] == "готово"
    assert result["warnings"] == []
    board.close()


def test_create_falls_back_to_the_default_status_with_a_warning() -> None:
    bodies: list[dict] = []
    board = _board(_handler(bodies))

    result = board.create(DOC, title="Новая задача", target="нет такого", project="PRI")

    assert "status" not in bodies[0]
    assert result["target_resolved"] == DEFAULT_STATUS
    assert result["warnings"] and "нет такого" in result["warnings"][0]
    board.close()


def test_create_without_a_target_does_not_request_statuses() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return _handler([])(request)

    board = _board(handler)
    result = board.create(DOC, title="Новая задача", target=None, project="PRI")

    assert seen == ["POST /api/v2/list/901/task"]
    assert result["target_resolved"] == DEFAULT_STATUS
    assert result["warnings"] == []
    board.close()


def test_unavailable_statuses_do_not_block_creation() -> None:
    bodies: list[dict] = []
    board = _board(_handler(bodies, list_status=403))

    result = board.create(DOC, title="Новая задача", target="готово", project="PRI")

    assert "status" not in bodies[0]
    assert result["key"]
    assert any("permission" in warning for warning in result["warnings"])
    assert TOKEN not in repr(result)
    board.close()


def test_custom_task_id_from_the_response_becomes_the_key() -> None:
    created = {
        "id": "5ef",
        "custom_id": "PRI-100",
        "status": {"status": DEFAULT_STATUS, "type": "open"},
        "url": "https://app.clickup.com/t/5ef",
    }
    board = _board(_handler([], created=created))

    result = board.create(DOC, title="Новая задача", target=None, project="PRI")

    assert result["key"] == "PRI-100"
    assert result["board_id"] == "5ef"
    board.close()


@pytest.mark.parametrize(
    ("title", "list_id"),
    [("", "901"), ("   ", "901"), ("Новая задача", "")],
)
def test_missing_required_input_is_a_configuration_error(title: str, list_id: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=LIST_PAYLOAD)

    board = _board(handler, list_id=list_id)
    with pytest.raises(BoardProviderError) as exc_info:
        board.create(DOC, title=title, target=None, project="PRI")

    assert exc_info.value.category == "configuration"
    assert requests == []
    board.close()


def test_response_without_a_task_id_is_reported() -> None:
    board = _board(_handler([], created={"status": {"status": DEFAULT_STATUS}}))

    with pytest.raises(BoardProviderError) as exc_info:
        board.create(DOC, title="Новая задача", target=None, project="PRI")

    assert exc_info.value.category == "unsupported"
    board.close()


def test_numeric_project_selects_the_list_for_creation() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={"id": "5ef", "status": {"status": DEFAULT_STATUS}, "url": "https://x.test/5ef"},
        )

    board = _board(handler, list_id="")
    board.create(DOC, title="Новая задача", target=None, project="777")

    assert seen == ["/api/v2/list/777/task"]
    board.close()
