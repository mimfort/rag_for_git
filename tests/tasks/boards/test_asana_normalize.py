"""Нормализация задачи Asana: html_notes → markdown, подзадачи, вложения, meta без I/O."""
from __future__ import annotations

import httpx

from reviewer.tasks.boards.asana import AsanaBoard

BASE = "https://app.asana.test/api/1.0"
PROJECT_GID = "1201234567890"
SECRET = "asana-secret-token"
GID = "1207654320001"
FILE_URL = "https://asana-files.test/private/spec.txt?sig=short-lived"

HTML_NOTES = (
    "<body><h1>Проблема</h1>Клиент видит <strong>ошибку</strong> и "
    '<em>таймаут</em><ul><li>шаг один</li><li>шаг два</li></ul>'
    '<a href="https://docs.test/spec">спека</a></body>'
)


def _task(**over: object) -> dict:
    task = {
        "gid": GID,
        "name": "Задача 1",
        "html_notes": HTML_NOTES,
        "notes": "plain fallback",
        "completed": False,
        "modified_at": "2026-07-23T09:12:00.000Z",
        "permalink_url": f"https://app.asana.test/0/{PROJECT_GID}/{GID}",
        "num_subtasks": 0,
        "memberships": [
            {"project": {"gid": PROJECT_GID}, "section": {"gid": "5001", "name": "Todo"}}
        ],
    }
    task.update(over)
    return task


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


def _raw(board: AsanaBoard, **over: object):
    return board._raw_from_task(_task(**over), PROJECT_GID)


def _empty(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": [], "next_page": None})


def test_description_is_converted_from_asana_html_to_markdown() -> None:
    board = _board(_empty)
    result = board.normalize(_raw(board))

    description = result["description"]
    assert "<" not in description
    assert "## Проблема" in description
    assert "**ошибку**" in description
    assert "*таймаут*" in description
    assert "- шаг один" in description
    assert "[спека](https://docs.test/spec)" in description
    board.close()


def test_legacy_paragraph_markup_is_not_left_in_description() -> None:
    board = _board(_empty)
    result = board.normalize(_raw(board, html_notes="<body><p>Абзац</p><p>Второй</p></body>"))

    assert "<p>" not in result["description"]
    assert result["description"] == "Абзац\n\nВторой"
    board.close()


def test_plain_notes_are_used_when_html_notes_is_absent() -> None:
    board = _board(_empty)
    result = board.normalize(_raw(board, html_notes=None, notes="Просто текст"))

    assert result["description"] == "Просто текст"
    board.close()


def test_subtasks_become_criteria_and_subtask_links() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/subtasks"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"gid": "9001", "name": "Подзадача A", "completed": False},
                        {"gid": "9002", "name": "Подзадача B", "completed": True},
                    ],
                    "next_page": None,
                },
            )
        return httpx.Response(200, json={"data": [], "next_page": None})

    board = _board(handler)
    result = board.normalize(_raw(board, num_subtasks=2))

    assert result["criteria"] == ["Подзадача A", "Подзадача B"]
    subtask_links = [link for link in result["links"] if link["type"] == "subtask"]
    assert subtask_links == [
        {"type": "subtask", "key": "ASN-9001", "title": "Подзадача A"},
        {"type": "subtask", "key": "ASN-9002", "title": "Подзадача B"},
    ]
    assert requests[0].url.path == f"/api/1.0/tasks/{GID}/subtasks"
    board.close()


def test_subtasks_are_not_requested_when_the_task_has_none() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [], "next_page": None})

    board = _board(handler)
    result = board.normalize(_raw(board, num_subtasks=0))

    assert result["criteria"] == []
    assert all(not call.url.path.endswith("/subtasks") for call in requests)
    board.close()


def test_attachments_are_listed_and_text_is_extracted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/1.0/attachments":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "gid": "7001",
                            "name": "spec.txt",
                            "size": 24,
                            "download_url": FILE_URL,
                            "permanent_url": "https://app.asana.test/app/asana/-/get_asset",
                        }
                    ],
                    "next_page": None,
                },
            )
        if request.url.host == "asana-files.test":
            return httpx.Response(200, text="Критерий из вложения")
        return httpx.Response(200, json={"data": [], "next_page": None})

    board = _board(handler)
    result = board.normalize(_raw(board))

    assert result["attachments"] == [
        {
            "name": "spec.txt",
            "mime_type": None,
            "size": 24,
            "content_text": "Критерий из вложения",
        }
    ]
    assert result["warnings"] == []
    board.close()


def test_attachment_download_never_sends_the_asana_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/1.0/attachments":
            return httpx.Response(
                200,
                json={
                    "data": [{"gid": "7001", "name": "spec.txt", "download_url": FILE_URL}],
                    "next_page": None,
                },
            )
        if request.url.host == "asana-files.test":
            return httpx.Response(200, text="Критерий из вложения")
        return httpx.Response(200, json={"data": [], "next_page": None})

    board = _board(handler)
    board.normalize(_raw(board))

    download = next(call for call in seen if call.url.host == "asana-files.test")
    assert "authorization" not in {name.lower() for name in download.headers}
    board.close()


def test_unsupported_and_off_scheme_attachments_keep_metadata_with_warning() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/1.0/attachments":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"gid": "7001", "name": "archive.zip", "download_url": FILE_URL},
                        {
                            "gid": "7002",
                            "name": "notes.md",
                            "download_url": "http://insecure.test/notes.md",
                        },
                        {"gid": "7003", "name": "linked.md", "download_url": None},
                    ],
                    "next_page": None,
                },
            )
        return httpx.Response(200, json={"data": [], "next_page": None})

    board = _board(handler)
    result = board.normalize(_raw(board))

    assert [item["name"] for item in result["attachments"]] == [
        "archive.zip",
        "notes.md",
        "linked.md",
    ]
    assert all(item["content_text"] is None for item in result["attachments"])
    assert len(result["warnings"]) == 3
    board.close()


def test_normalize_meta_makes_no_requests_and_drops_details() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [], "next_page": None})

    board = _board(handler)
    result = board.normalize_meta(_raw(board, num_subtasks=3))

    assert calls == []
    assert result["criteria"] == []
    assert result["attachments"] == []
    assert result["key"] == f"ASN-{GID}"
    assert result["aliases"] == [GID]
    assert "<" not in result["description"]
    board.close()


def test_status_reports_done_for_completed_tasks_and_section_otherwise() -> None:
    board = _board(_empty)
    assert board.normalize_meta(_raw(board))["status"] == "Todo"
    assert board.normalize_meta(_raw(board, completed=True))["status"] == "done"
    board.close()


def test_url_prefers_the_asana_permalink_and_project_is_the_key_prefix() -> None:
    board = _board(_empty)
    result = board.normalize_meta(_raw(board))

    assert result["url"] == f"https://app.asana.test/0/{PROJECT_GID}/{GID}"
    assert result["project"] == "ASN"
    board.close()


def test_url_falls_back_to_the_configured_template() -> None:
    board = _board(_empty)
    result = board.normalize_meta(_raw(board, permalink_url=None))

    assert result["url"] == f"https://app.asana.test/task/ASN-{GID}"
    board.close()


def test_related_keys_from_the_description_become_links() -> None:
    board = _board(_empty)
    result = board.normalize_meta(_raw(board, html_notes="<body>см. ASN-9099</body>"))

    assert {"type": "related", "key": "ASN-9099", "title": ""} in result["links"]
    board.close()
