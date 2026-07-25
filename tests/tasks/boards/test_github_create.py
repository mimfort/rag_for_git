"""Создание issue в GitHub: метка/milestone как цель, fallback с предупреждением."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.github import GitHubIssuesBoard

TOKEN = "github-secret-token"
DOC = "## Проблема\n\nПадает синк."


def _board(handler, **kwargs) -> GitHubIssuesBoard:
    params: dict[str, Any] = {
        "token": TOKEN,
        "repo": "acme/widgets",
        "key_prefix": "PRI",
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _seconds: None,
    }
    params.update(kwargs)
    return GitHubIssuesBoard(**params)


def _handler(created: list[dict], *, number: int = 77):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/labels"):
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=[{"name": "done"}] if page == 1 else [])
        if request.method == "GET" and path.endswith("/milestones"):
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(
                200,
                json=[{"number": 3, "title": "v1.0"}] if page == 1 else [],
            )
        if request.method == "POST" and path == "/repos/acme/widgets/issues":
            created.append(json.loads(request.content))
            return httpx.Response(
                201,
                json={
                    "number": number,
                    "node_id": f"I_kw{number}",
                    "html_url": f"https://github.com/acme/widgets/issues/{number}",
                },
            )
        return httpx.Response(404, json={})

    return handle


def test_create_applies_label_target_and_reports_it() -> None:
    created: list[dict] = []

    result = _board(_handler(created)).create(
        DOC,
        title="Синк падает",
        target="label:done",
        project="acme/widgets",
    )

    assert created == [{"title": "Синк падает", "body": DOC, "labels": ["done"]}]
    assert result["key"] == "PRI-77"
    assert result["board_id"] == "77"
    assert result["url"] == "https://github.com/acme/widgets/issues/77"
    assert result["target_resolved"] == "label:done"
    assert result["warnings"] == []


def test_create_accepts_a_human_label_and_a_milestone_number() -> None:
    created: list[dict] = []
    board = _board(_handler(created))

    by_label = board.create(DOC, title="A", target="done", project=None)
    by_milestone = board.create(DOC, title="B", target="v1.0", project=None)

    assert by_label["target_resolved"] == "label:done"
    assert created[0]["labels"] == ["done"]
    assert by_milestone["target_resolved"] == "milestone:3"
    assert created[1]["milestone"] == 3
    assert "labels" not in created[1]


def test_missing_target_falls_back_to_the_default_place_with_a_warning() -> None:
    created: list[dict] = []

    result = _board(_handler(created)).create(
        DOC,
        title="Синк падает",
        target="Нет такой цели",
        project=None,
    )

    assert created == [{"title": "Синк падает", "body": DOC}]
    assert result["key"] == "PRI-77"
    assert result["target_resolved"] != "Нет такой цели"
    assert result["target_resolved"] is None
    assert result["warnings"]


def test_create_without_target_asks_no_discovery() -> None:
    seen: list[str] = []
    created: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return _handler(created)(request)

    result = _board(handler).create(DOC, title="Синк падает", target=None, project=None)

    assert seen == ["/repos/acme/widgets/issues"]
    assert result["target_resolved"] is None
    assert result["warnings"] == []


def test_repository_and_title_are_required() -> None:
    created: list[dict] = []

    with pytest.raises(BoardProviderError) as no_repo:
        _board(_handler(created), repo="").create(DOC, title="X", target=None, project=None)
    with pytest.raises(BoardProviderError) as no_title:
        _board(_handler(created)).create(DOC, title="  ", target=None, project=None)

    assert no_repo.value.category == "configuration"
    assert no_title.value.category == "configuration"
    assert created == []


def test_response_without_issue_number_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"html_url": "https://github.test/x"})
        return httpx.Response(404, json={})

    with pytest.raises(BoardProviderError) as exc_info:
        _board(handler).create(DOC, title="X", target=None, project=None)
    assert exc_info.value.category == "unsupported"
