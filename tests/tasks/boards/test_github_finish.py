"""Закрытие issue GitHub: идемпотентная PR-ссылка, state=closed, метка/milestone."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.github import GitHubIssuesBoard

TOKEN = "github-secret-token"
PR_URL = "https://github.com/acme/widgets/pull/7"


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


class _Repo:
    """Состояние issue в памяти: PATCH виден следующему GET (проверка идемпотентности)."""

    def __init__(self) -> None:
        self.issue: dict[str, Any] = {
            "number": 7,
            "node_id": "I_kw7",
            "html_url": "https://github.com/acme/widgets/issues/7",
            "title": "Синк падает",
            "body": "Описание",
            "state": "open",
            "labels": [],
            "milestone": None,
        }
        self.patches: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
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
        if request.method == "GET" and path == "/repos/acme/widgets/issues/7":
            return httpx.Response(200, json=self.issue)
        if request.method == "PATCH" and path == "/repos/acme/widgets/issues/7":
            payload = json.loads(request.content)
            self.patches.append(payload)
            if "labels" in payload:
                self.issue["labels"] = [{"name": name} for name in payload["labels"]]
            if "milestone" in payload:
                self.issue["milestone"] = {"number": payload["milestone"], "title": "v1.0"}
            for field in ("body", "state", "state_reason"):
                if field in payload:
                    self.issue[field] = payload[field]
            return httpx.Response(200, json=self.issue)
        return httpx.Response(404, json={})


def test_finish_is_idempotent_for_link_state_and_label() -> None:
    repo = _Repo()
    board = _board(repo.handle)

    first = board.finish("PRI-7", PR_URL, note="Проверено", target="label:done")
    second = board.finish("PRI-7", PR_URL, note="Проверено", target="label:done")

    assert first["pr_link_added"] is True
    assert first["done_set"] is True
    assert first["already_closed"] is False
    assert first["target_resolved"] == "label:done"
    assert first["board_id"] == "7"
    assert second["pr_link_added"] is False
    assert second["done_set"] is False
    assert second["already_closed"] is True
    assert len(repo.patches) == 1
    assert repo.patches[0]["state"] == "closed"
    assert repo.patches[0]["state_reason"] == "completed"
    assert repo.patches[0]["labels"] == ["done"]
    assert PR_URL in repo.issue["body"]
    assert "Проверено" in repo.issue["body"]
    assert repo.issue["body"].startswith("Описание")


def test_milestone_target_is_applied_once() -> None:
    repo = _Repo()
    board = _board(repo.handle)

    first = board.finish("PRI-7", PR_URL, target="milestone:3")
    second = board.finish("PRI-7", PR_URL, target="milestone:3")

    assert repo.patches[0]["milestone"] == 3
    assert first["done_set"] is True
    assert second["done_set"] is False
    assert second["already_closed"] is True


def test_state_reason_target_closes_without_touching_labels() -> None:
    repo = _Repo()

    result = _board(repo.handle).finish("PRI-7", PR_URL, target="not_planned")

    assert repo.patches[0]["state_reason"] == "not_planned"
    assert "labels" not in repo.patches[0]
    assert result["target_resolved"] == "not_planned"
    assert result["warnings"] == []


def test_unknown_target_still_closes_the_issue_with_a_warning() -> None:
    repo = _Repo()

    result = _board(repo.handle).finish("PRI-7", PR_URL, target="Нет такой цели")

    assert repo.patches[0]["state"] == "closed"
    assert result["done_set"] is True
    assert result["target_resolved"] is None
    assert result["warnings"]


def test_mark_done_false_only_appends_the_pr_link() -> None:
    repo = _Repo()

    result = _board(repo.handle).finish("PRI-7", PR_URL, mark_done=False)

    assert repo.patches == [{"body": repo.issue["body"]}]
    assert result["pr_link_added"] is True
    assert result["done_set"] is False
    assert repo.issue["state"] == "open"


def test_already_closed_issue_only_gets_the_link() -> None:
    repo = _Repo()
    repo.issue["state"] = "closed"

    result = _board(repo.handle).finish("PRI-7", PR_URL, target=None)

    assert "state" not in repo.patches[0]
    assert result["pr_link_added"] is True
    assert result["done_set"] is False
    assert result["already_closed"] is False


def test_failed_patch_is_reported_without_leaking_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            return httpx.Response(403, json={"token": TOKEN})
        return _Repo().handle(request)

    result = _board(handler).finish("PRI-7", PR_URL, target=None)

    assert result["pr_link_added"] is False
    assert result["done_set"] is False
    assert result["already_closed"] is False
    assert result["warnings"]
    assert TOKEN not in repr(result)


def test_finish_requires_a_repository_and_a_numeric_key() -> None:
    repo = _Repo()

    with pytest.raises(BoardProviderError) as no_repo:
        _board(repo.handle, repo="").finish("PRI-7", PR_URL)
    with pytest.raises(BoardProviderError) as bad_key:
        _board(repo.handle).finish("не-ключ", PR_URL)

    assert no_repo.value.category == "configuration"
    assert bad_key.value.category == "configuration"
