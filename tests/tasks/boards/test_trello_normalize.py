"""Нормализация карточки Trello: markdown-инвариант, чеклисты, вложения, meta без I/O."""
from __future__ import annotations

import httpx

from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.boards.trello import TrelloBoard

API_KEY = "trello-app-key"
TOKEN = "trello-secret-token"
BOARD = "5f00000000000000000000b0"
CARD = "5f00000000000000000000c3"
DOWNLOAD = f"https://trello.com/1/cards/{CARD}/attachments/att-1/download/spec.txt"

CHECKLISTS = [
    {
        "id": "chk-1",
        "name": "Критерии приёмки",
        "checkItems": [
            {"id": "ci-1", "name": "Тесты проходят", "state": "incomplete"},
            {"id": "ci-2", "name": "Документация обновлена", "state": "complete"},
        ],
    }
]
ATTACHMENTS = [
    {
        "id": "att-1",
        "name": "spec.txt",
        "url": DOWNLOAD,
        "mimeType": "text/plain",
        "bytes": 40,
        "isUpload": True,
    },
    {
        "id": "att-2",
        "name": "design.pdf",
        "url": "https://files.example/design.pdf",
        "mimeType": "application/pdf",
        "bytes": 10,
        "isUpload": False,
    },
]


def _raw() -> RawTask:
    return RawTask(
        key="TRL-3",
        project_code="TRL-3",
        title="Задача 3",
        description="## Проблема\n\nСмотри также TRL-9",
        status="Backlog",
        subtask_ids=[],
        timestamp=1784797200000,
        board_id=CARD,
        provider_data={
            "id_short": 3,
            "id_list": "list-1",
            "id_board": BOARD,
            "short_link": "sh000003",
            "short_url": "https://trello.com/c/sh000003",
            "closed": False,
        },
    )


def _board(handler) -> TrelloBoard:
    return TrelloBoard(
        api_key=API_KEY,
        api_token=TOKEN,
        board_id=BOARD,
        key_prefix="TRL",
        key_pattern=r"TRL-\d+",
        url_template="https://trello.test/task/{code}",
        attachment_max_bytes=1000,
        attachment_timeout=1.0,
        attachment_store_chars=1000,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    )


def _handler(requests: list[httpx.Request], *, checklists_status: int = 200):
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/checklists"):
            if checklists_status != 200:
                return httpx.Response(checklists_status, json={"token": TOKEN})
            return httpx.Response(200, json=CHECKLISTS)
        if path.endswith("/attachments"):
            return httpx.Response(200, json=ATTACHMENTS)
        if request.url.host == "trello.com" and path.endswith("/download/spec.txt"):
            if request.headers.get("Authorization") != (
                f'OAuth oauth_consumer_key="{API_KEY}", oauth_token="{TOKEN}"'
            ):
                return httpx.Response(401, json={"token": TOKEN})
            return httpx.Response(200, text="Критерий из вложения")
        return httpx.Response(404, json={})

    return handle


def test_description_stays_native_markdown_and_checklists_become_criteria_and_links() -> None:
    requests: list[httpx.Request] = []
    result = _board(_handler(requests)).normalize(_raw())

    assert result["description"] == "## Проблема\n\nСмотри также TRL-9"
    assert "<p>" not in result["description"]
    assert result["criteria"] == ["Тесты проходят", "Документация обновлена"]
    subtasks = [link for link in result["links"] if link["type"] == "subtask"]
    assert [link["key"] for link in subtasks] == ["ci-1", "ci-2"]
    assert subtasks[0]["title"] == "Тесты проходят"
    assert {"type": "related", "key": "TRL-9", "title": ""} in result["links"]
    assert result["aliases"] == [CARD, "sh000003"]
    assert result["url"] == "https://trello.com/c/sh000003"
    assert result["project"] == "TRL"
    assert [request.url.path for request in requests[:2]] == [
        f"/1/cards/{CARD}/checklists",
        f"/1/cards/{CARD}/attachments",
    ]


def test_uploaded_attachment_text_is_fetched_with_the_trello_oauth_header() -> None:
    requests: list[httpx.Request] = []
    result = _board(_handler(requests)).normalize(_raw())

    spec = result["attachments"][0]
    assert spec["name"] == "spec.txt"
    assert spec["content_text"] == "Критерий из вложения"
    assert spec["mime_type"] == "text/plain"
    downloads = [request for request in requests if request.url.path.endswith("spec.txt")]
    assert len(downloads) == 1
    assert str(downloads[0].url).startswith(DOWNLOAD)
    assert downloads[0].headers["Authorization"] == (
        f'OAuth oauth_consumer_key="{API_KEY}", oauth_token="{TOKEN}"'
    )


def test_offhost_attachment_link_is_metadata_only_and_never_requested() -> None:
    requests: list[httpx.Request] = []
    result = _board(_handler(requests)).normalize(_raw())

    design = result["attachments"][1]
    assert design == {
        "name": "design.pdf",
        "mime_type": "application/pdf",
        "size": 10,
        "content_text": None,
    }
    assert any("design.pdf" in warning for warning in result["warnings"])
    assert all(request.url.host != "files.example" for request in requests)


def test_unavailable_checklists_degrade_to_a_warning_without_raising() -> None:
    requests: list[httpx.Request] = []
    result = _board(_handler(requests, checklists_status=403)).normalize(_raw())

    assert result["criteria"] == []
    assert any("чеклист" in warning for warning in result["warnings"])
    assert TOKEN not in repr(result)
    assert result["attachments"][0]["name"] == "spec.txt"


def test_normalize_meta_makes_no_requests_and_keeps_flat_metadata() -> None:
    requests: list[httpx.Request] = []
    result = _board(_handler(requests)).normalize_meta(_raw())

    assert requests == []
    assert result["key"] == "TRL-3"
    assert result["criteria"] == []
    assert result["attachments"] == []
    assert result["status"] == "Backlog"
    assert result["links"] == [{"type": "related", "key": "TRL-9", "title": ""}]
    assert set(result) == {
        "key",
        "aliases",
        "title",
        "description",
        "criteria",
        "status",
        "url",
        "links",
        "project",
        "attachments",
        "warnings",
    }


def test_url_falls_back_to_the_configured_template() -> None:
    raw = _raw()
    raw.provider_data["short_url"] = ""
    result = _board(_handler([])).normalize_meta(raw)

    assert result["url"] == "https://trello.test/task/TRL-3"
