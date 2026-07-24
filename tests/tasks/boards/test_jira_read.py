from __future__ import annotations

import base64
import dataclasses
import json
from pathlib import Path

import httpx
import pytest

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
    ],
)
def test_constructor_accepts_only_direct_https_site_url(base_url: str) -> None:
    with pytest.raises(BoardProviderError) as exc_info:
        _board(lambda _: httpx.Response(200, json={}), base_url=base_url)
    assert exc_info.value.category == "configuration"
    assert "jira-secret-token" not in repr(exc_info.value)


def test_basic_auth_and_enhanced_search_pagination() -> None:
    pages = [_fixture("search-page-1.json"), _fixture("search-page-2.json")]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert base64.b64decode(request.headers["Authorization"].removeprefix("Basic ")).decode() == (
            "bot@example.test:jira-secret-token"
        )
        return httpx.Response(200, json=pages[len(requests) - 1])

    rows = list(_board(handler).iter_raw("PRI", None))

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

