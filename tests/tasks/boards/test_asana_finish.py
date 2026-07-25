"""Закрытие задачи Asana: completed=true, PR-ссылка в html_notes, секция, идемпотентность."""
from __future__ import annotations

import json

import httpx
import pytest

from reviewer.tasks.boards.asana import AsanaBoard
from reviewer.tasks.boards.errors import BoardProviderError

BASE = "https://app.asana.test/api/1.0"
PROJECT_GID = "1201234567890"
SECRET = "asana-secret-token"
GID = "1207654320002"
KEY = f"ASN-{GID}"
PR_URL = "https://github.test/pull/7"


class _Board:
    """Изменяемое состояние одной задачи Asana + обработчик MockTransport."""

    def __init__(
        self,
        *,
        html_notes: str = "<body>Описание задачи</body>",
        notes: str = "Описание задачи",
        completed: bool = False,
        section: dict | None = None,
        sections: list[dict] | None = None,
        notes_write_status: int = 200,
    ) -> None:
        self.html_notes = html_notes
        self.notes = notes
        self.completed = completed
        self.section = section or {"gid": "5001", "name": "Todo"}
        self.sections = sections if sections is not None else [
            {"gid": "5001", "name": "Todo"},
            {"gid": "5003", "name": "Done"},
        ]
        self.notes_write_status = notes_write_status
        self.requests: list[httpx.Request] = []

    def task(self) -> dict:
        return {
            "gid": GID,
            "name": "Задача 2",
            "html_notes": self.html_notes,
            "notes": self.notes,
            "completed": self.completed,
            "modified_at": "2026-07-23T09:12:00.000Z",
            "permalink_url": f"https://app.asana.test/0/{PROJECT_GID}/{GID}",
            "num_subtasks": 0,
            "memberships": [{"project": {"gid": PROJECT_GID}, "section": self.section}],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == f"/api/1.0/tasks/{GID}":
            return httpx.Response(200, json={"data": self.task()})
        if request.method == "GET" and path.endswith("/sections"):
            return httpx.Response(200, json={"data": self.sections, "next_page": None})
        if request.method == "PUT" and path == f"/api/1.0/tasks/{GID}":
            data = json.loads(request.content)["data"]
            if "html_notes" in data:
                if self.notes_write_status != 200:
                    return httpx.Response(self.notes_write_status, json={"token": SECRET})
                self.html_notes = data["html_notes"]
            if "completed" in data:
                self.completed = bool(data["completed"])
            return httpx.Response(200, json={"data": self.task()})
        if request.method == "POST" and path == "/api/1.0/sections/5003/addTask":
            self.section = {"gid": "5003", "name": "Done"}
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(404, json={})

    def provider(self, **kwargs: object) -> AsanaBoard:
        options: dict = {
            "access_token": SECRET,
            "api_base": BASE,
            "project_gid": PROJECT_GID,
            "key_prefix": "ASN",
            "key_pattern": r"ASN-\d+",
            "url_template": "",
            "attachment_max_bytes": 1000,
            "attachment_timeout": 1.0,
            "attachment_store_chars": 1000,
            "transport": httpx.MockTransport(self.handler),
            "sleeper": lambda _: None,
        }
        options.update(kwargs)
        return AsanaBoard(**options)  # type: ignore[arg-type]

    def writes(self) -> list[dict]:
        return [
            json.loads(call.content)["data"]
            for call in self.requests
            if call.method == "PUT"
        ]


def test_finish_appends_the_pr_link_and_marks_the_task_completed() -> None:
    board = _Board()
    provider = board.provider()

    result = provider.finish(KEY, PR_URL, note="Проверено")

    assert result["pr_link_added"] is True
    assert result["done_set"] is True
    assert result["already_closed"] is False
    assert result["key"] == KEY
    assert result["board_id"] == GID
    assert result["warnings"] == []
    assert board.completed is True
    assert board.html_notes.startswith("<body>Описание задачи")
    assert board.html_notes.endswith("</body>")
    assert f'<a href="{PR_URL}">{PR_URL}</a>' in board.html_notes
    assert "Проверено" in board.html_notes
    assert "<p>" not in board.html_notes
    assert "<br" not in board.html_notes
    assert board.writes() == [
        {"html_notes": board.html_notes},
        {"completed": True},
    ]
    provider.close()


def test_finish_is_idempotent_on_a_second_identical_call() -> None:
    board = _Board()
    provider = board.provider()

    first = provider.finish(KEY, PR_URL, note="Проверено", target="5003")
    calls_after_first = len(board.requests)
    second = provider.finish(KEY, PR_URL, note="Проверено", target="5003")

    assert first["pr_link_added"] is True
    assert first["done_set"] is True
    assert second["pr_link_added"] is False
    assert second["done_set"] is False
    assert second["already_closed"] is True
    assert second["section_moved"] is False
    writes = [
        call
        for call in board.requests[calls_after_first:]
        if call.method in {"PUT", "POST"}
    ]
    assert writes == []
    provider.close()


def test_finish_moves_the_task_into_the_done_section() -> None:
    board = _Board()
    provider = board.provider()

    result = provider.finish(KEY, PR_URL, target="Done")

    add_task = next(
        call for call in board.requests if call.url.path.endswith("/addTask")
    )
    assert add_task.url.path == "/api/1.0/sections/5003/addTask"
    assert json.loads(add_task.content) == {"data": {"task": GID}}
    assert result["section_moved"] is True
    assert result["done_set"] is True
    assert board.section["gid"] == "5003"
    provider.close()


def test_finish_does_not_move_a_task_already_in_the_done_section() -> None:
    board = _Board(section={"gid": "5003", "name": "Done"})
    provider = board.provider()

    result = provider.finish(KEY, PR_URL, target="5003")

    assert all(not call.url.path.endswith("/addTask") for call in board.requests)
    assert result["section_moved"] is False
    assert result["done_set"] is True
    provider.close()


def test_finish_warns_about_a_missing_section_but_still_closes_the_task() -> None:
    board = _Board()
    provider = board.provider()

    result = provider.finish(KEY, PR_URL, target="Missing")

    assert result["done_set"] is True
    assert result["section_moved"] is False
    assert result["warnings"] == ["секция 'Missing' не найдена — не применена"]
    provider.close()


def test_finish_escapes_the_pr_url_and_the_note() -> None:
    board = _Board()
    provider = board.provider()

    provider.finish(
        KEY,
        'https://github.test/pull/7?a=1&b="2"',
        note="<script>alert(1)</script>",
    )

    assert "<script>" not in board.html_notes
    assert "&lt;script&gt;" in board.html_notes
    assert 'a=1&amp;b=&quot;2&quot;' in board.html_notes
    provider.close()


def test_finish_converts_plain_notes_into_escaped_asana_html() -> None:
    board = _Board(html_notes="", notes="Описание с 5 < 7")
    provider = board.provider()

    provider.finish(KEY, PR_URL)

    assert board.html_notes.startswith("<body>Описание с 5 &lt; 7")
    provider.close()


def test_finish_without_mark_done_only_appends_the_link() -> None:
    board = _Board()
    provider = board.provider()

    result = provider.finish(KEY, PR_URL, mark_done=False)

    assert result["pr_link_added"] is True
    assert result["done_set"] is False
    assert board.completed is False
    assert board.writes() == [{"html_notes": board.html_notes}]
    provider.close()


def test_finish_reports_already_closed_when_nothing_changes() -> None:
    board = _Board(
        html_notes=f'<body>Описание<a href="{PR_URL}">{PR_URL}</a></body>',
        completed=True,
    )
    provider = board.provider()

    result = provider.finish(KEY, PR_URL)

    assert result == {
        "key": KEY,
        "board_id": GID,
        "done_set": False,
        "pr_link_added": False,
        "already_closed": True,
        "section_moved": False,
        "warnings": [],
    }
    assert board.writes() == []
    provider.close()


def test_finish_still_closes_the_task_when_the_notes_update_fails() -> None:
    board = _Board(notes_write_status=400)
    provider = board.provider()

    result = provider.finish(KEY, PR_URL)

    assert result["pr_link_added"] is False
    assert result["done_set"] is True
    assert result["already_closed"] is False
    assert result["warnings"]
    assert SECRET not in repr(result)
    assert board.completed is True
    provider.close()


def test_finish_with_a_key_without_a_gid_is_a_configuration_error() -> None:
    board = _Board()
    provider = board.provider()

    with pytest.raises(BoardProviderError) as exc_info:
        provider.finish("ASN-not-a-gid", PR_URL)

    assert exc_info.value.category == "configuration"
    assert board.requests == []
    provider.close()
