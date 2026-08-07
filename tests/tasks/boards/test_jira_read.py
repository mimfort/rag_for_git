from __future__ import annotations

import base64
import dataclasses
import json
from pathlib import Path

import httpx
import pytest

from reviewer.config.task_board import TaskSyncFilter
from reviewer.tasks.boards.base import TaskListing
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.jira import JiraCloudBoard

FIXTURES = Path(__file__).parents[2] / "fixtures" / "jira"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _board(handler, *, base_url: str = "https://acme.atlassian.net") -> JiraCloudBoard:
    return JiraCloudBoard(
        base_url=base_url,
        email="bot@example.test",
        api_token="jira-secret-token",
        key_pattern=r"[A-Z]+-\d+",
        issue_type="10001",
        attachment_max_bytes=100,
        attachment_timeout=1.0,
        attachment_store_chars=10,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://acme.atlassian.net",
        "https://acme.atlassian.net/rest/api/3",
        "https://user@acme.atlassian.net",
        "https://acme.atlassian.net?token=secret",
        "https://attacker.example",
        "https://atlassian.net",
        "https://acme.atlassian.net.attacker.example",
        "https://nested.acme.atlassian.net",
        "https://acme.atlassian.net:444",
        "https://acme.atlassian.net:0",
        "https://-acme.atlassian.net",
        "https://acme-.atlassian.net",
    ],
)
def test_constructor_accepts_only_direct_https_site_url(base_url: str) -> None:
    requests: list[httpx.Request] = []

    with pytest.raises(BoardProviderError) as exc_info:
        _board(
            lambda request: requests.append(request) or httpx.Response(200, json={}),
            base_url=base_url,
        )
    assert exc_info.value.category == "configuration"
    assert "jira-secret-token" not in repr(exc_info.value)
    assert requests == []


def test_hostile_jira_origin_is_rejected_before_authenticated_client_construction(
    monkeypatch,
) -> None:
    constructions: list[dict] = []
    monkeypatch.setattr(
        "reviewer.tasks.boards.jira.httpx.Client",
        lambda **kwargs: constructions.append(kwargs),
    )

    with pytest.raises(BoardProviderError):
        _board(
            lambda _request: httpx.Response(200, json={}),
            base_url="https://acme.atlassian.net.attacker.example",
        )

    assert constructions == []


@pytest.mark.parametrize(
    "base_url",
    ["https://acme.atlassian.net", "https://acme.atlassian.net:443"],
)
def test_constructor_accepts_exact_atlassian_tenant_on_effective_port_443(
    base_url: str,
) -> None:
    provider = _board(lambda _request: httpx.Response(200, json={}), base_url=base_url)
    provider.close()


def test_basic_auth_and_enhanced_search_pagination() -> None:
    pages = [_fixture("search-page-1.json"), _fixture("search-page-2.json")]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert base64.b64decode(request.headers["Authorization"].removeprefix("Basic ")).decode() == (
            "bot@example.test:jira-secret-token"
        )
        return httpx.Response(200, json=pages[len(requests) - 1])

    listing = _board(handler).iter_raw(
        "PRI",
        None,
        sync_filter=TaskSyncFilter(max_age_days=30, include_archived=False),
        now_ms=123,
    )
    rows = list(listing)

    assert isinstance(listing, TaskListing)
    assert [row.key for row in rows] == ["PRI-1", "PRI-2"]
    first, second = (json.loads(request.content) for request in requests)
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/rest/api/3/search/jql"
    assert first == {
        "jql": 'project = "PRI" ORDER BY updated ASC',
        "fields": JiraCloudBoard._FIELDS.split(","),
        "maxResults": 100,
    }
    assert second["nextPageToken"] == "page-2"
    assert rows[0].timestamp == 1784797200000
    assert rows[1].timestamp == 1784797200000
    assert all(row.archived is None and row.terminal is None for row in rows)
    assert listing.stats.filtered_by_age == 0
    assert listing.stats.filtered_archived == 0
    assert listing.stats.warnings == []


@pytest.mark.parametrize("updated", [None, "not a timestamp"])
def test_missing_or_invalid_updated_timestamp_stays_nullable(updated: object) -> None:
    raw = JiraCloudBoard._raw_from_issue(
        {"id": "1", "key": "PRI-1", "fields": {"updated": updated}}
    )

    assert raw.timestamp is None
    assert raw.archived is None
    assert raw.terminal is None


def test_limit_stops_without_loading_the_next_page() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_fixture("search-page-1.json"))

    assert [row.key for row in _board(handler).iter_raw("PRI", 1)] == ["PRI-1"]
    assert calls == 1


def test_fetch_one_and_search_share_mapper_and_404_is_none() -> None:
    issue = _fixture("issue.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/PRI-404"):
            return httpx.Response(404, json={"secret": "must not leak"})
        if request.url.path.endswith("/PRI-1"):
            return httpx.Response(200, json=issue)
        return httpx.Response(200, json={"issues": [issue], "isLast": True})

    board = _board(handler)
    raw = next(iter(board.iter_raw("PRI", 1)))
    one = board.fetch_one("PRI-1")

    assert one is not None
    assert dataclasses.asdict(raw) == dataclasses.asdict(one)
    assert board.fetch_one("PRI-404") is None
