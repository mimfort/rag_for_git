"""Нормализация ClickUp: markdown-инвариант, подзадачи, вложения, дешёвый normalize_meta."""
from __future__ import annotations

import httpx

from reviewer.tasks.boards.clickup import ClickUpBoard

TOKEN = "pk_clickup_normalize_secret"
ATTACHMENT = {
    "id": "att-1",
    "title": "spec.txt",
    "url": "https://attachments.clickup.com/att-1/spec.txt",
    "size": "24",
    "mimetype": "text/plain",
}


def _board(handler, **kwargs) -> ClickUpBoard:
    params: dict = {
        "token": TOKEN,
        "list_id": "901",
        "key_prefix": "PRI",
        "key_pattern": r"PRI-\d+",
        "attachment_max_bytes": 1000,
        "attachment_timeout": 1.0,
        "attachment_store_chars": 1000,
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _: None,
    }
    params.update(kwargs)
    return ClickUpBoard(**params)


def _task(native: str, **overrides) -> dict:
    task: dict = {
        "id": native,
        "name": "Родитель",
        "description": "plain текст",
        "markdown_description": "## Заголовок\n\nСвязано с PRI-77",
        "status": {"status": "к выполнению", "type": "custom", "orderindex": 0},
        "date_updated": "1784797200000",
        "url": f"https://app.clickup.com/t/{native}",
        "list": {"id": "901"},
    }
    task.update(overrides)
    return task


def _page(*tasks: dict) -> dict:
    return {"tasks": list(tasks), "last_page": True}


def test_normalize_keeps_markdown_and_maps_subtasks_links_and_criteria() -> None:
    parent = _task("2kv", attachments=[ATTACHMENT])
    child = _task("3ab", name="Ребёнок", parent="2kv", markdown_description="дочка")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/spec.txt"):
            return httpx.Response(200, text="Критерий из вложения")
        return httpx.Response(200, json=_page(parent, child))

    board = _board(handler)
    raw = next(iter(board.iter_raw("PRI", 1)))
    brief = board.normalize(raw)

    child_key = f"PRI-{int('3ab', 36)}"
    assert "<p>" not in brief["description"]
    assert brief["description"].startswith("## Заголовок")
    assert brief["criteria"] == ["Ребёнок"]
    assert {"type": "subtask", "key": child_key, "title": "Ребёнок"} in brief["links"]
    assert {"type": "related", "key": "PRI-77", "title": ""} in brief["links"]
    assert brief["key"] == f"PRI-{int('2kv', 36)}"
    assert brief["project"] == "PRI"
    assert brief["status"] == "к выполнению"
    assert brief["url"] == "https://app.clickup.com/t/2kv"
    assert "2kv" in brief["aliases"]
    assert brief["attachments"] == [
        {
            "name": "spec.txt",
            "mime_type": "text/plain",
            "size": 24,
            "content_text": "Критерий из вложения",
        }
    ]
    assert brief["warnings"] == []
    board.close()


def test_custom_id_becomes_the_key_and_native_id_stays_an_alias() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page(_task("4cd", custom_id="PRI-42")))

    board = _board(handler)
    brief = board.normalize(next(iter(board.iter_raw("PRI", 1))))

    assert brief["key"] == "PRI-42"
    assert brief["aliases"] == ["4cd"]
    board.close()


def test_missing_markdown_description_falls_back_to_plain_text_with_warning() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        task = _task("2kv")
        task.pop("markdown_description")
        return httpx.Response(200, json=_page(task))

    board = _board(handler)
    brief = board.normalize(next(iter(board.iter_raw("PRI", 1))))

    assert brief["description"] == "plain текст"
    assert any("markdown_description" in warning for warning in brief["warnings"])
    board.close()


def test_attachment_download_carries_no_authorization_header() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/spec.txt"):
            seen.append({key.lower(): value for key, value in request.headers.items()})
            return httpx.Response(200, text="Критерий из вложения")
        return httpx.Response(200, json=_page(_task("2kv", attachments=[ATTACHMENT])))

    board = _board(handler)
    board.normalize(next(iter(board.iter_raw("PRI", 1))))

    assert seen and "authorization" not in seen[0]
    board.close()


def test_offhost_attachment_is_skipped_with_a_warning() -> None:
    foreign = {**ATTACHMENT, "url": "https://attacker.example/spec.txt"}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page(_task("2kv", attachments=[foreign])))

    board = _board(handler)
    brief = board.normalize(next(iter(board.iter_raw("PRI", 1))))

    assert brief["attachments"] == []
    assert any("домен" in warning for warning in brief["warnings"])
    board.close()


def test_oversized_and_unsupported_attachments_stay_metadata_only() -> None:
    big = {**ATTACHMENT, "title": "big.txt", "size": 5000}
    binary = {**ATTACHMENT, "title": "diagram.psd", "mimetype": "image/vnd.adobe.photoshop"}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page(_task("2kv", attachments=[big, binary])))

    board = _board(handler)
    brief = board.normalize(next(iter(board.iter_raw("PRI", 1))))

    assert brief["attachments"] == []
    assert len(brief["warnings"]) == 2
    board.close()


def test_unreadable_attachment_keeps_metadata_and_warns() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/spec.txt"):
            return httpx.Response(403, text="private attachment links")
        return httpx.Response(200, json=_page(_task("2kv", attachments=[ATTACHMENT])))

    board = _board(handler)
    brief = board.normalize(next(iter(board.iter_raw("PRI", 1))))

    assert brief["attachments"][0]["name"] == "spec.txt"
    assert brief["attachments"][0]["content_text"] is None
    assert any("содержимое" in warning for warning in brief["warnings"])
    board.close()


def test_normalize_meta_spends_no_http_and_resolves_no_details() -> None:
    requests: list[httpx.Request] = []
    parent = _task("2kv", attachments=[ATTACHMENT])
    child = _task("3ab", name="Ребёнок", parent="2kv")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_page(parent, child))

    board = _board(handler)
    raw = next(iter(board.iter_raw("PRI", 1)))
    requests.clear()
    brief = board.normalize_meta(raw)

    assert requests == []
    assert brief["criteria"] == []
    assert brief["attachments"] == []
    assert brief["key"] == raw.key
    assert brief["status"] == "к выполнению"
    assert brief["url"] == "https://app.clickup.com/t/2kv"
    assert any(link["type"] == "subtask" for link in brief["links"])
    board.close()


def test_url_template_is_used_when_the_task_has_no_url() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        task = _task("2kv")
        task.pop("url")
        return httpx.Response(200, json=_page(task))

    board = _board(handler, url_template="https://board.test/task/{code}")
    brief = board.normalize(next(iter(board.iter_raw("PRI", 1))))

    assert brief["url"] == f"https://board.test/task/PRI-{int('2kv', 36)}"
    board.close()
