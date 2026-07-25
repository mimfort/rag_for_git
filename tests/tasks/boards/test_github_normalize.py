"""Нормализация задач GitHub Issues: markdown как есть, чеклист, sub-issues, вложения."""
from __future__ import annotations

from typing import Any

import httpx

from reviewer.tasks.boards.github import GitHubIssuesBoard

TOKEN = "github-secret-token"
BODY = """## Проблема

Падает синк, см. PRI-42.

- [x] #9 Починить курсор
- [ ] Добавить тест

![скрин](https://user-images.githubusercontent.com/1/screen.png)
[spec.txt](https://github.com/user-attachments/files/123/spec.txt)
"""


def _board(handler, **kwargs) -> GitHubIssuesBoard:
    params: dict[str, Any] = {
        "token": TOKEN,
        "repo": "acme/widgets",
        "key_prefix": "PRI",
        "key_pattern": r"PRI-\d+",
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _seconds: None,
    }
    params.update(kwargs)
    return GitHubIssuesBoard(**params)


def _issue(**overrides: Any) -> dict[str, Any]:
    issue = {
        "number": 7,
        "node_id": "I_kw7",
        "html_url": "https://github.com/acme/widgets/issues/7",
        "title": "Синк падает",
        "body": BODY,
        "state": "open",
        "updated_at": "2026-07-23T09:15:00Z",
        "labels": [{"name": "bug"}],
        "milestone": {"number": 3, "title": "v1.0"},
        "sub_issues_summary": {"total": 0, "completed": 0, "percent_completed": 0},
    }
    issue.update(overrides)
    return issue


def _raw(board: GitHubIssuesBoard):
    return next(iter(board.iter_raw(None, 1)))


def test_normalize_keeps_native_markdown_and_maps_flat_fields() -> None:
    board = _board(lambda _r: httpx.Response(200, json=[_issue()]))

    brief = board.normalize(_raw(board))

    assert brief["description"] == BODY
    assert "<p>" not in brief["description"]
    assert brief["key"] == "PRI-7"
    assert brief["title"] == "Синк падает"
    assert brief["status"] == "open"
    assert brief["url"] == "https://github.com/acme/widgets/issues/7"
    assert brief["project"] == "PRI"
    assert brief["aliases"] == ["acme/widgets#7", "I_kw7"]


def test_checklist_becomes_criteria_and_subtask_links() -> None:
    board = _board(lambda _r: httpx.Response(200, json=[_issue()]))

    brief = board.normalize(_raw(board))

    assert brief["criteria"] == ["#9 Починить курсор", "Добавить тест"]
    subtasks = [link for link in brief["links"] if link["type"] == "subtask"]
    assert {"type": "subtask", "key": "PRI-9", "title": "#9 Починить курсор"} in subtasks
    assert any(link["key"] == "" and link["title"] == "Добавить тест" for link in subtasks)
    assert {"type": "related", "key": "PRI-42", "title": ""} in brief["links"]


def test_attachments_are_metadata_only_with_a_warning() -> None:
    board = _board(lambda _r: httpx.Response(200, json=[_issue()]))

    brief = board.normalize(_raw(board))

    assert [att["name"] for att in brief["attachments"]] == ["screen.png", "spec.txt"]
    assert brief["attachments"][0]["content_text"] is None
    assert brief["attachments"][0]["url"].startswith("https://user-images.")
    assert brief["warnings"]


def test_native_sub_issues_extend_criteria_and_links() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/sub_issues"):
            return httpx.Response(
                200,
                json=[{"number": 11, "title": "Подзадача", "state": "open"}],
            )
        return httpx.Response(
            200,
            json=[_issue(sub_issues_summary={"total": 1, "completed": 0})],
        )

    board = _board(handler)
    brief = board.normalize(_raw(board))

    assert "/repos/acme/widgets/issues/7/sub_issues" in calls
    assert {"type": "subtask", "key": "PRI-11", "title": "Подзадача"} in brief["links"]
    assert "Подзадача" in brief["criteria"]


def test_sub_issues_failure_is_fail_soft() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sub_issues"):
            return httpx.Response(404, json={"token": TOKEN})
        return httpx.Response(
            200,
            json=[_issue(sub_issues_summary={"total": 2, "completed": 0})],
        )

    board = _board(handler)
    brief = board.normalize(_raw(board))

    assert brief["criteria"] == ["#9 Починить курсор", "Добавить тест"]
    assert any("sub-issues" in warning for warning in brief["warnings"])
    assert TOKEN not in repr(brief)


def test_normalize_meta_spends_no_http_and_drops_details() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json=[_issue(sub_issues_summary={"total": 3, "completed": 0})],
        )

    board = _board(handler)
    raw = _raw(board)
    calls.clear()

    brief = board.normalize_meta(raw)

    assert calls == []
    assert brief["criteria"] == []
    assert brief["attachments"] == []
    assert brief["key"] == "PRI-7"
    assert brief["url"] == "https://github.com/acme/widgets/issues/7"
    assert brief["project"] == "PRI"


def test_url_is_rebuilt_when_the_payload_has_no_html_url() -> None:
    board = _board(lambda _r: httpx.Response(200, json=[_issue(html_url="")]))

    assert board.normalize_meta(_raw(board))["url"] == (
        "https://github.com/acme/widgets/issues/7"
    )


def test_enterprise_attachment_host_is_recognised() -> None:
    body = "[spec.txt](https://ghe.example/user-attachments/files/1/spec.txt)"
    board = _board(
        lambda _r: httpx.Response(200, json=[_issue(body=body)]),
        api_base="https://ghe.example/api/v3",
    )

    brief = board.normalize(_raw(board))

    assert [att["name"] for att in brief["attachments"]] == ["spec.txt"]
    assert brief["url"] == "https://github.com/acme/widgets/issues/7"
