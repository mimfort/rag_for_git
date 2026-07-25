"""Нормализация задачи Yandex Tracker: YFM → markdown, связи, вложения, meta без I/O.

Связи читаются отдельным ``GET /v3/issues/{key}/links``
(https://yandex.ru/support/tracker/en/concepts/issues/get-links): ``direction=outward``
означает, что задача из запроса — главная, то есть ``object`` для типа ``subtask``
является подзадачей.
"""
from __future__ import annotations

import httpx

from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.boards.yandex_tracker import YandexTrackerBoard

TOKEN = "yandex-tracker-secret-token"

YFM_DESCRIPTION = (
    "Описание TREK-1 ((https://wiki.test/spec Спека))\n\n"
    "%%(python)\nprint(1)\n%%\n\n"
    "<{Детали\nсвязано с TREK-9\n}>"
)


def _board(handler, **kwargs) -> YandexTrackerBoard:
    options: dict = {
        "token": TOKEN,
        "org_id": "org-42",
        "key_pattern": r"TREK-\d+",
        "attachment_max_bytes": 1000,
        "attachment_timeout": 1.0,
        "attachment_store_chars": 1000,
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _: None,
    }
    options.update(kwargs)
    return YandexTrackerBoard(**options)


def _issue(**over) -> dict:
    issue = {
        "id": "60001",
        "key": "TREK-1",
        "summary": "Задача 1",
        "description": YFM_DESCRIPTION,
        "status": {"id": "1", "key": "open", "display": "Открыт"},
        "queue": {"id": "3", "key": "TREK", "display": "Trek"},
        "updatedAt": "2026-07-23T09:11:12.347+0000",
        "attachments": [
            {
                "id": "1",
                "name": "spec.txt",
                "content": "https://api.tracker.yandex.net/v3/issues/TREK-1/attachments/1/spec.txt",
                "mimetype": "text/plain",
                "size": 24,
            }
        ],
    }
    issue.update(over)
    return issue


_LINKS = [
    {
        "type": {"id": "subtask", "inward": "Подзадача", "outward": "Родительская задача"},
        "direction": "outward",
        "object": {"key": "TREK-2", "display": "Подзадача про кэш"},
        "status": {"key": "open", "display": "Открыт"},
    },
    {
        "type": {"id": "subtask", "inward": "Подзадача", "outward": "Родительская задача"},
        "direction": "inward",
        "object": {"key": "TREK-5", "display": "Родитель"},
    },
    {
        "type": {"id": "relates", "inward": "Связана с", "outward": "Связана с"},
        "direction": "outward",
        "object": {"key": "TREK-6", "display": "Смежная"},
    },
]


def _handler(*, links=None, attachment_text: str = "Критерий из вложения"):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/links"):
            return httpx.Response(200, json=_LINKS if links is None else links)
        if path.endswith("/spec.txt"):
            return httpx.Response(200, text=attachment_text)
        if path == "/v3/issues/_search":
            return httpx.Response(200, json=[_issue()])
        return httpx.Response(200, json=_issue())

    return handle


def _raw(board: YandexTrackerBoard) -> RawTask:
    return next(iter(board.iter_raw("TREK", 1)))


def test_description_is_converted_from_yfm_to_markdown() -> None:
    board = _board(_handler())

    brief = board.normalize(_raw(board))

    assert "<p>" not in brief["description"]
    assert "[Спека](https://wiki.test/spec)" in brief["description"]
    assert "```python\nprint(1)\n```" in brief["description"]
    assert "**Детали**" in brief["description"]
    assert "%%" not in brief["description"]
    board.close()


def test_links_map_subtask_parent_and_related_with_criteria() -> None:
    board = _board(_handler())

    brief = board.normalize(_raw(board))

    assert {(link["type"], link["key"]) for link in brief["links"]} >= {
        ("subtask", "TREK-2"),
        ("parent", "TREK-5"),
        ("related", "TREK-6"),
    }
    assert brief["criteria"] == ["Подзадача про кэш"]
    board.close()


def test_related_keys_from_description_are_added_once() -> None:
    board = _board(_handler())

    brief = board.normalize(_raw(board))
    related = [link for link in brief["links"] if link["key"] == "TREK-9"]

    assert [link["type"] for link in related] == ["related"]
    board.close()


def test_key_from_description_is_not_duplicated_when_already_linked() -> None:
    board = _board(_handler())

    brief = board.normalize(_raw(board))

    assert [link["key"] for link in brief["links"]].count("TREK-2") == 1
    board.close()


def test_attachment_is_downloaded_through_the_authorized_client() -> None:
    seen: list[httpx.Request] = []
    inner = _handler()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return inner(request)

    board = _board(handler)
    brief = board.normalize(_raw(board))

    assert brief["attachments"] == [
        {
            "name": "spec.txt",
            "mime_type": "text/plain",
            "size": 24,
            "content_text": "Критерий из вложения",
        }
    ]
    download = next(request for request in seen if request.url.path.endswith("/spec.txt"))
    assert download.headers["Authorization"] == f"OAuth {TOKEN}"
    assert download.headers["X-Org-ID"] == "org-42"
    board.close()


def test_off_host_attachment_is_skipped_with_a_warning() -> None:
    board = _board(_handler())
    raw = _raw(board)
    raw.attachments = [
        {"name": "evil.txt", "mime": "text/plain", "size": 10, "url": "https://evil.test/evil.txt"}
    ]

    brief = board.normalize(raw)

    assert brief["attachments"] == []
    assert any("evil.txt" in warning for warning in brief["warnings"])
    board.close()


def test_unavailable_links_are_fail_soft() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/links"):
            return httpx.Response(403, json={"token": TOKEN})
        if request.url.path.endswith("/spec.txt"):
            return httpx.Response(200, text="x")
        return httpx.Response(200, json=[_issue()])

    board = _board(handler)
    brief = board.normalize(_raw(board))

    assert brief["links"] == [{"type": "related", "key": "TREK-9", "title": ""}]
    assert brief["warnings"]
    assert TOKEN not in repr(brief)
    board.close()


def test_normalize_meta_makes_no_requests_and_keeps_flat_fields() -> None:
    seen: list[httpx.Request] = []
    inner = _handler()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return inner(request)

    board = _board(handler)
    raw = _raw(board)
    seen.clear()

    brief = board.normalize_meta(raw)

    assert seen == []
    assert brief["criteria"] == []
    assert brief["attachments"] == []
    assert brief["key"] == "TREK-1"
    assert brief["status"] == "Открыт"
    assert brief["project"] == "TREK"
    assert brief["url"] == "https://tracker.yandex.ru/TREK-1"
    assert "<p>" not in brief["description"]
    board.close()


def test_url_template_overrides_the_default_web_url() -> None:
    board = _board(_handler(), url_template="https://tracker.test/issue/{code}")

    brief = board.normalize_meta(_raw(board))

    assert brief["url"] == "https://tracker.test/issue/TREK-1"
    board.close()


def test_brief_shape_matches_the_task_brief_contract() -> None:
    board = _board(_handler())

    brief = board.normalize(_raw(board))

    assert set(brief) == {
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
    board.close()
