"""Discovery целей GitHub Issues: метки и milestone'ы в общей форме targets."""
from __future__ import annotations

from typing import Any

import httpx

from reviewer.tasks.boards.github import GitHubIssuesBoard

TOKEN = "github-secret-token"

LABELS = [{"id": 1, "name": "bug"}, {"id": 2, "name": "done"}]
MILESTONES = [
    {"number": 3, "title": "v1.0", "state": "open"},
    {"number": 4, "title": "Готово", "state": "closed"},
]


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


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/repos/acme/widgets/labels":
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=LABELS if page == 1 else [])
    if request.url.path == "/repos/acme/widgets/milestones":
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=MILESTONES if page == 1 else [])
    return httpx.Response(404, json={})


def test_list_targets_returns_labels_and_milestones_in_one_list() -> None:
    result = _board(_handler).list_targets("acme/widgets")

    assert set(result) == {"targets", "options", "warnings"}
    assert result["warnings"] == []
    assert [(target["id"], target["label"]) for target in result["targets"]] == [
        ("label:bug", "bug"),
        ("label:done", "done"),
        ("milestone:3", "v1.0"),
        ("milestone:4", "Готово"),
    ]
    assert all("create" in target["purposes"] for target in result["targets"])
    assert all("done" in target["purposes"] for target in result["targets"])
    assert {"id", "label", "purposes"} <= set(result["targets"][0])


def test_milestones_are_requested_in_every_state() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return _handler(request)

    _board(handler).list_targets(None)

    milestone_params = [params for params in seen if "state" in params]
    assert milestone_params[0]["state"] == "all"
    assert milestone_params[0]["per_page"] == "100"


def test_options_expose_repo_and_key_prefix() -> None:
    options = _board(_handler).list_targets(None)["options"]

    assert [option["key"] for option in options] == ["repo", "key_prefix"]
    assert options[0]["required_for"] == ["sync", "create", "finish"]
    assert options[1]["required_for"] == ["sync"]


def test_missing_repository_is_a_warning_not_an_error() -> None:
    result = _board(_handler, repo="").list_targets(None)

    assert result["targets"] == []
    assert result["warnings"]


def test_unavailable_labels_degrade_to_milestones_with_a_warning() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/labels"):
            return httpx.Response(403, json={"token": TOKEN})
        return _handler(request)

    result = _board(handler).list_targets(None)

    assert [target["id"] for target in result["targets"]] == ["milestone:3", "milestone:4"]
    assert result["warnings"]
    assert TOKEN not in repr(result)
