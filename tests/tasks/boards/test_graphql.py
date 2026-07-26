"""Тесты GraphQL-фасада для адаптеров досок."""
from __future__ import annotations

import json

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.graphql import GraphQLClient
from reviewer.tasks.boards.http import BoardHttpClient


def _gql(handler, *, secrets=("linear-secret",), attempts=1) -> GraphQLClient:
    client = httpx.Client(
        base_url="https://api.linear.app",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "linear-secret"},
    )
    http = BoardHttpClient(client, attempts=attempts, sleeper=lambda _: None, secrets=secrets)
    return GraphQLClient(http, secrets=secrets)


def test_execute_posts_query_and_returns_data():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"data": {"viewer": {"id": "u1"}}})

    client = _gql(handler)
    assert client.execute("query Me { viewer { id } }") == {"viewer": {"id": "u1"}}
    assert seen[0]["query"].startswith("query Me")
    assert seen[0]["variables"] == {}


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("AUTHENTICATION_ERROR", "authentication"),
        ("FORBIDDEN", "permission"),
        ("RATELIMITED", "rate_limit"),
        ("ENTITY_NOT_FOUND", "not_found"),
        ("INTERNAL_SERVER_ERROR", "unsupported"),
    ],
)
def test_graphql_errors_map_to_categories(code: str, category: str):
    client = _gql(
        lambda request: httpx.Response(
            200,
            json={"errors": [{"message": "boom", "extensions": {"code": code}}]},
        )
    )
    with pytest.raises(BoardProviderError) as exc_info:
        client.execute("query { viewer { id } }")
    assert exc_info.value.category == category


def test_graphql_error_text_never_contains_secret():
    client = _gql(
        lambda request: httpx.Response(
            200,
            json={"errors": [{"message": "token linear-secret rejected"}]},
        )
    )
    with pytest.raises(BoardProviderError) as exc_info:
        client.execute("query { viewer { id } }")
    assert "linear-secret" not in f"{exc_info.value!s}{exc_info.value!r}"


def test_paginate_follows_page_info_end_cursor():
    pages = [
        {
            "data": {
                "issues": {
                    "nodes": [{"id": "a"}, {"id": "b"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cur-1"},
                }
            }
        },
        {
            "data": {
                "issues": {
                    "nodes": [{"id": "c"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        },
    ]
    cursors: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        cursors.append(body["variables"].get("after"))
        return httpx.Response(200, json=pages[len(cursors) - 1])

    client = _gql(handler)
    query = (
        "query Issues($after: String) { issues(after: $after) "
        "{ nodes { id } pageInfo { hasNextPage endCursor } } }"
    )
    nodes = list(client.paginate(query, {"first": 50}, connection=lambda data: data["issues"]))
    assert [node["id"] for node in nodes] == ["a", "b", "c"]
    assert cursors == [None, "cur-1"]
