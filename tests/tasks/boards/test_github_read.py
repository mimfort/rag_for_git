"""Чтение доски GitHub Issues: пагинация, маппинг RawTask, лимит, семантика ошибок."""
from __future__ import annotations

import dataclasses
from typing import Any

import httpx
import pytest

from reviewer.config.task_board import TaskSyncFilter
from reviewer.tasks.boards.base import TaskListing
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.github import GitHubIssuesBoard

TOKEN = "github-secret-token"


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


def _issue(number: int, **overrides: Any) -> dict[str, Any]:
    issue = {
        "number": number,
        "node_id": f"I_kw{number}",
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
        "title": f"Задача {number}",
        "body": f"Описание задачи {number}",
        "state": "open",
        "state_reason": None,
        "updated_at": "2026-07-23T09:15:00Z",
        "labels": [{"name": "bug"}],
        "milestone": None,
        "sub_issues_summary": {"total": 0, "completed": 0, "percent_completed": 0},
    }
    issue.update(overrides)
    return issue


def test_pagination_walks_pages_with_exact_params_and_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = int(request.url.params["page"])
        issues = [_issue(number) for number in range(1, 101)] if page == 1 else [_issue(101)]
        return httpx.Response(200, json=issues)

    listing = _board(handler).iter_raw(
        "acme/widgets",
        None,
        sync_filter=TaskSyncFilter(max_age_days=30, include_archived=False),
        now_ms=123,
    )
    rows = list(listing)

    assert isinstance(listing, TaskListing)
    assert len(rows) == 101
    assert len(seen) == 2
    assert [request.url.path for request in seen] == ["/repos/acme/widgets/issues"] * 2
    assert dict(seen[0].url.params) == {
        "state": "all",
        "sort": "updated",
        "direction": "desc",
        "per_page": "100",
        "page": "1",
    }
    assert dict(seen[1].url.params)["page"] == "2"
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert seen[0].headers["Accept"] == "application/vnd.github+json"
    assert seen[0].headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert listing.stats.filtered_by_age == 0
    assert listing.stats.filtered_archived == 0
    assert listing.stats.warnings == []


def test_pull_requests_are_never_returned_as_tasks() -> None:
    payload = [
        _issue(1),
        _issue(2, pull_request={"url": "https://api.github.com/repos/acme/widgets/pulls/2"}),
        _issue(3),
    ]

    rows = list(_board(lambda _r: httpx.Response(200, json=payload)).iter_raw(None, None))

    assert [row.key for row in rows] == ["PRI-1", "PRI-3"]


def test_raw_task_maps_key_number_and_epoch_ms_timestamp() -> None:
    payload = [_issue(7, state="closed", state_reason="completed")]

    row = next(iter(_board(lambda _r: httpx.Response(200, json=payload)).iter_raw(None, None)))

    assert row.key == "PRI-7"
    assert row.project_code == "PRI-7"
    assert row.board_id == "7"
    assert row.title == "Задача 7"
    assert row.description == "Описание задачи 7"
    assert row.status == "closed"
    assert row.timestamp == 1784798100000
    assert row.archived is None
    assert row.terminal is True
    assert row.provider_data["number"] == 7
    assert row.provider_data["node_id"] == "I_kw7"
    assert row.provider_data["html_url"] == "https://github.com/acme/widgets/issues/7"
    assert row.provider_data["state_reason"] == "completed"
    assert row.provider_data["repo"] == "acme/widgets"


def test_lifecycle_state_is_tri_state_and_archived_is_unknown() -> None:
    absent = _issue(3)
    absent.pop("state")
    payload = [_issue(1, state="closed"), _issue(2, state="open"), absent]

    rows = list(_board(lambda _r: httpx.Response(200, json=payload)).iter_raw(None, None))

    assert [row.terminal for row in rows] == [True, False, None]
    assert all(row.archived is None for row in rows)


@pytest.mark.parametrize("updated_at", [None, "not a timestamp"])
def test_missing_or_invalid_updated_timestamp_stays_nullable(updated_at: object) -> None:
    row = next(
        iter(
            _board(
                lambda _r: httpx.Response(200, json=[_issue(1, updated_at=updated_at)])
            ).iter_raw(None, None)
        )
    )

    assert row.timestamp is None


def test_key_prefix_falls_back_to_repository_name() -> None:
    board = _board(lambda _r: httpx.Response(200, json=[_issue(5)]), key_prefix="")

    assert next(iter(board.iter_raw(None, None))).key == "WIDGETS-5"


def test_limit_stops_without_loading_the_next_page() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[_issue(number) for number in range(1, 101)])

    assert [row.key for row in _board(handler).iter_raw(None, 2)] == ["PRI-1", "PRI-2"]
    assert calls == 1


def test_board_argument_overrides_the_configured_repository() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=[])

    list(_board(handler).iter_raw("other/repo", None))

    assert seen == ["/repos/other/repo/issues"]


@pytest.mark.parametrize(
    "repo",
    ["", "widgets", "acme/widgets/extra", "acme/../secrets", "acme/wid gets", "acme/"],
)
def test_iter_raw_without_a_valid_repository_is_a_configuration_error(repo: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    board = _board(handler, repo=repo)

    with pytest.raises(BoardProviderError) as exc_info:
        list(board.iter_raw(None, None))
    assert exc_info.value.category == "configuration"
    assert seen == []


def test_fetch_one_shares_the_iter_mapper_and_404_is_none() -> None:
    issue = _issue(7)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/widgets/issues/404":
            return httpx.Response(404, json={"token": TOKEN})
        if request.url.path == "/repos/acme/widgets/issues/7":
            return httpx.Response(200, json=issue)
        return httpx.Response(200, json=[issue])

    board = _board(handler)
    raw = next(iter(board.iter_raw(None, 1)))
    one = board.fetch_one("PRI-7")

    assert one is not None
    assert dataclasses.asdict(one) == dataclasses.asdict(raw)
    assert board.fetch_one("PRI-404") is None
    assert board.fetch_one("#7") == one


def _identity_handler(**repo_fields: Any):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "robot", "id": 1, "name": "Robot"})
        if request.url.path == "/repos/acme/widgets":
            payload = {
                "full_name": "acme/widgets",
                "has_issues": True,
                "permissions": {"push": True, "pull": True},
            }
            payload.update(repo_fields)
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})

    return handle


def test_validate_connection_reports_identity_and_capabilities() -> None:
    result = _board(_identity_handler()).validate_connection("acme/widgets")

    assert set(result) == {"status", "identity", "project", "capabilities", "warnings"}
    assert result["status"] == "ok"
    assert result["identity"] == {"login": "robot", "id": 1, "name": "Robot"}
    assert result["project"] == "acme/widgets"
    assert result["capabilities"] == {"read": True, "create": True, "finish": True}
    assert result["warnings"] == []
    assert TOKEN not in repr(result)


def test_validate_connection_warns_about_read_only_access_and_disabled_issues() -> None:
    result = _board(
        _identity_handler(has_issues=False, permissions={"pull": True}),
    ).validate_connection(None)

    assert result["status"] == "ok"
    assert result["capabilities"] == {"read": True, "create": False, "finish": False}
    assert len(result["warnings"]) == 2


def test_validate_connection_without_repository_is_ok_with_a_warning() -> None:
    result = _board(_identity_handler(), repo="").validate_connection(None)

    assert result["status"] == "ok"
    assert result["capabilities"] == {"read": False, "create": False, "finish": False}
    assert result["warnings"]


@pytest.mark.parametrize(
    ("status", "category"),
    [(403, "permission"), (404, "not_found"), (401, "authentication")],
)
def test_validate_connection_maps_transport_status(status: int, category: str) -> None:
    board = _board(lambda _r: httpx.Response(status, json={"token": TOKEN}))

    with pytest.raises(BoardProviderError) as exc_info:
        board.validate_connection("acme/widgets")
    assert exc_info.value.category == category
    assert TOKEN not in f"{exc_info.value!s}{exc_info.value!r}"


def test_exhausted_rate_limit_403_is_retryable_rate_limit() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1"},
            json={"token": TOKEN},
        )

    with pytest.raises(BoardProviderError) as exc_info:
        list(_board(handler).iter_raw(None, None))
    assert exc_info.value.category == "rate_limit"
    assert exc_info.value.retryable is True
    assert calls == 3
    assert TOKEN not in f"{exc_info.value!s}{exc_info.value!r}"


def test_plain_403_stays_a_permission_error() -> None:
    board = _board(lambda _r: httpx.Response(403, json={"token": TOKEN}))

    with pytest.raises(BoardProviderError) as exc_info:
        list(board.iter_raw(None, None))
    assert exc_info.value.category == "permission"


@pytest.mark.parametrize(
    "api_base",
    ["http://ghe.example/api/v3", "https://api.github.com?token=x", "not-a-url", ""],
)
def test_api_base_must_be_an_https_url(api_base: str) -> None:
    with pytest.raises(BoardProviderError) as exc_info:
        _board(lambda _r: httpx.Response(200, json=[]), api_base=api_base)
    assert exc_info.value.category == "configuration"
    assert TOKEN not in repr(exc_info.value)


def test_enterprise_api_base_is_used_verbatim() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=[])

    board = _board(handler, api_base="https://ghe.example/api/v3")
    list(board.iter_raw(None, None))

    assert seen[0].host == "ghe.example"
    assert seen[0].path == "/api/v3/repos/acme/widgets/issues"
