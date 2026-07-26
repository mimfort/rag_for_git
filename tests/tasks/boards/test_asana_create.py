"""Создание задачи Asana: markdown → html_notes, размещение в секции, fallback."""
from __future__ import annotations

import json

import httpx
import pytest

from reviewer.tasks.boards.asana import AsanaBoard
from reviewer.tasks.boards.errors import BoardProviderError

BASE = "https://app.asana.test/api/1.0"
PROJECT_GID = "1201234567890"
SECRET = "asana-secret-token"
NEW_GID = "1207654329999"

DOC_MD = (
    "## Проблема\n\nКлиент видит **ошибку** и [спеку](https://docs.test/spec)\n\n"
    "### Детали\n\n- шаг один\n- шаг два\n\n```\nprint(1)\n```"
)


def _board(handler, **kwargs: object) -> AsanaBoard:
    options: dict = {
        "access_token": SECRET,
        "api_base": BASE,
        "project_gid": PROJECT_GID,
        "key_prefix": "ASN",
        "key_pattern": r"ASN-\d+",
        "url_template": "https://app.asana.test/task/{code}",
        "attachment_max_bytes": 1000,
        "attachment_timeout": 1.0,
        "attachment_store_chars": 1000,
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _: None,
    }
    options.update(kwargs)
    return AsanaBoard(**options)  # type: ignore[arg-type]


def _created(**over: object) -> dict:
    task = {
        "gid": NEW_GID,
        "name": "Новая задача",
        "permalink_url": f"https://app.asana.test/0/{PROJECT_GID}/{NEW_GID}",
        "memberships": [
            {"project": {"gid": PROJECT_GID}, "section": {"gid": "5001", "name": "Todo"}}
        ],
    }
    task.update(over)
    return task


def _handler(
    requests: list[httpx.Request],
    *,
    sections: list[dict] | None = None,
    created: dict | None = None,
):
    rows = sections if sections is not None else [
        {"gid": "5001", "name": "Todo"},
        {"gid": "5003", "name": "Done"},
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/sections"):
            return httpx.Response(200, json={"data": rows, "next_page": None})
        if request.method == "POST" and request.url.path == "/api/1.0/tasks":
            return httpx.Response(
                201,
                json={"data": _created() if created is None else created},
            )
        return httpx.Response(404, json={})

    return handle


def _payload(requests: list[httpx.Request]) -> dict:
    post = next(
        call
        for call in requests
        if call.method == "POST" and call.url.path == "/api/1.0/tasks"
    )
    return json.loads(post.content)["data"]


def test_create_sends_name_notes_project_and_section_membership() -> None:
    requests: list[httpx.Request] = []
    board = _board(_handler(requests))

    result = board.create(DOC_MD, title="Новая задача", target="5003", project="ASN")

    data = _payload(requests)
    assert data["name"] == "Новая задача"
    assert data["projects"] == [PROJECT_GID]
    assert data["memberships"] == [{"project": PROJECT_GID, "section": "5003"}]
    assert result == {
        "key": f"ASN-{NEW_GID}",
        "url": f"https://app.asana.test/0/{PROJECT_GID}/{NEW_GID}",
        "board_id": NEW_GID,
        "target_resolved": "5003",
        "warnings": [],
    }
    board.close()


def test_create_converts_markdown_into_asana_flavoured_html() -> None:
    requests: list[httpx.Request] = []
    board = _board(_handler(requests))
    board.create(DOC_MD, title="Новая задача", target=None, project="ASN")

    notes = _payload(requests)["html_notes"]
    assert notes.startswith("<body>")
    assert notes.endswith("</body>")
    assert "<p>" not in notes
    assert "<br" not in notes
    assert "<h3>" not in notes
    assert "<h2>Проблема</h2>" in notes
    assert "<h2>Детали</h2>" in notes
    assert "<strong>ошибку</strong>" in notes
    assert '<a href="https://docs.test/spec">спеку</a>' in notes
    assert "<ul><li>шаг один</li><li>шаг два</li></ul>" in notes
    assert "<pre>print(1)</pre>" in notes
    assert "<code>" not in notes
    board.close()


def test_create_without_a_target_omits_memberships() -> None:
    requests: list[httpx.Request] = []
    board = _board(_handler(requests))
    result = board.create("текст", title="Новая задача", target=None, project="ASN")

    assert "memberships" not in _payload(requests)
    assert all(not call.url.path.endswith("/sections") for call in requests)
    assert result["target_resolved"] == "5001"
    assert result["warnings"] == []
    board.close()


def test_create_resolves_the_section_by_name() -> None:
    requests: list[httpx.Request] = []
    board = _board(_handler(requests))
    result = board.create("текст", title="Новая задача", target="Done", project="ASN")

    assert _payload(requests)["memberships"] == [
        {"project": PROJECT_GID, "section": "5003"}
    ]
    assert result["target_resolved"] == "5003"
    assert result["warnings"] == []
    board.close()


def test_create_falls_back_to_the_default_section_with_a_warning() -> None:
    requests: list[httpx.Request] = []
    board = _board(_handler(requests))
    result = board.create("текст", title="Новая задача", target="Missing", project="ASN")

    assert "memberships" not in _payload(requests)
    assert result["key"] == f"ASN-{NEW_GID}"
    assert result["target_resolved"] == "5001"
    assert result["warnings"] == ["секция 'Missing' не найдена — не применена"]
    board.close()


def test_create_warns_when_the_section_name_is_ambiguous() -> None:
    requests: list[httpx.Request] = []
    board = _board(
        _handler(
            requests,
            sections=[{"gid": "5003", "name": "Done"}, {"gid": "5004", "name": "Done"}],
        )
    )
    result = board.create("текст", title="Новая задача", target="Done", project="ASN")

    assert "memberships" not in _payload(requests)
    assert result["warnings"] == ["секция 'Done' неоднозначна — не применена"]
    board.close()


def test_create_survives_unreadable_sections_and_still_creates_the_task() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/sections"):
            return httpx.Response(403, json={"token": SECRET})
        return httpx.Response(201, json={"data": _created()})

    board = _board(handler)
    result = board.create("текст", title="Новая задача", target="Done", project="ASN")

    assert result["key"] == f"ASN-{NEW_GID}"
    assert result["warnings"]
    assert SECRET not in repr(result)
    board.close()


def test_create_without_a_project_gid_is_a_configuration_error() -> None:
    requests: list[httpx.Request] = []
    board = _board(_handler(requests), project_gid="")

    with pytest.raises(BoardProviderError) as exc_info:
        board.create("текст", title="Новая задача", target=None, project="ASN")

    assert exc_info.value.category == "configuration"
    assert requests == []
    board.close()


def test_create_response_without_a_gid_is_rejected() -> None:
    requests: list[httpx.Request] = []
    board = _board(_handler(requests, created={"permalink_url": "https://app.asana.test/0/1/2"}))

    with pytest.raises(BoardProviderError) as exc_info:
        board.create("текст", title="Новая задача", target=None, project="ASN")

    assert exc_info.value.category == "unsupported"
    assert SECRET not in repr(exc_info.value)
    board.close()


def test_create_url_falls_back_to_the_template_without_a_permalink() -> None:
    requests: list[httpx.Request] = []
    board = _board(_handler(requests, created=_created(permalink_url=None)))
    result = board.create("текст", title="Новая задача", target=None, project="ASN")

    assert result["url"] == f"https://app.asana.test/task/ASN-{NEW_GID}"
    board.close()
