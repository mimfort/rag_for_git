"""Чтение задач Linear: курсорная пагинация GraphQL, маппинг RawTask, limit."""
from __future__ import annotations

import dataclasses
import json

import httpx

from reviewer.config.task_board import TaskSyncFilter
from reviewer.tasks.boards.base import TaskListing
from reviewer.tasks.boards.linear import LinearBoard

SECRET = "linear-read-secret"


def _issue(number: int, *, updated: str = "2026-07-23T09:01:00.000Z") -> dict:
    """Узел issue из схемы Linear (поля — как их запрашивает адаптер)."""
    return {
        "id": f"issue-uuid-{number}",
        "identifier": f"ENG-{number}",
        "title": f"Задача {number}",
        "description": f"Описание **ENG-{number}**",
        "updatedAt": updated,
        "url": f"https://linear.app/acme/issue/ENG-{number}",
        "state": {"id": "state-backlog", "name": "Backlog", "type": "backlog"},
        "team": {"id": "team-uuid-1", "key": "ENG"},
        "children": {"nodes": [{"identifier": "ENG-9", "title": "Подзадача"}]},
        "attachments": {
            "nodes": [
                {
                    "title": "spec.txt",
                    "url": "https://uploads.linear.app/spec.txt",
                }
            ]
        },
    }


def _page(nodes: list[dict], *, cursor: str | None) -> dict:
    return {
        "data": {
            "issues": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor},
            }
        }
    }


def _board(handler) -> LinearBoard:
    return LinearBoard(
        api_key=SECRET,
        api_base="https://api.linear.app",
        key_pattern=r"ENG-\d+",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    )


def test_iter_raw_walks_cursor_pages_and_sends_api_key_without_bearer() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/graphql"
        assert request.headers["Authorization"] == SECRET
        bodies.append(json.loads(request.content.decode()))
        if len(bodies) == 1:
            return httpx.Response(200, json=_page([_issue(1), _issue(2)], cursor="cursor-1"))
        return httpx.Response(200, json=_page([_issue(3)], cursor=None))

    listing = _board(handler).iter_raw(
        "ENG",
        None,
        sync_filter=TaskSyncFilter(max_age_days=30, include_archived=False),
        now_ms=123,
    )
    rows = list(listing)

    assert isinstance(listing, TaskListing)
    assert [row.key for row in rows] == ["ENG-1", "ENG-2", "ENG-3"]
    assert len(bodies) == 2
    assert bodies[0]["variables"] == {
        "filter": {"team": {"key": {"eq": "ENG"}}},
        "first": 50,
        "after": None,
    }
    assert bodies[1]["variables"]["after"] == "cursor-1"
    assert "orderBy: updatedAt" in bodies[0]["query"]
    assert listing.stats.filtered_by_age == 0
    assert listing.stats.filtered_archived == 0
    assert listing.stats.warnings == []


def test_iter_raw_without_board_sends_no_team_filter() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode()))
        return httpx.Response(200, json=_page([_issue(1)], cursor=None))

    assert [row.key for row in _board(handler).iter_raw(None, None)] == ["ENG-1"]
    assert bodies[0]["variables"]["filter"] is None


def test_raw_task_maps_identifier_uuid_children_and_epoch_ms() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page([_issue(1)], cursor=None))

    row = next(iter(_board(handler).iter_raw("ENG", 1)))

    assert (row.key, row.project_code, row.board_id) == ("ENG-1", "ENG-1", "issue-uuid-1")
    assert row.title == "Задача 1"
    assert row.description == "Описание **ENG-1**"
    assert row.status == "Backlog"
    assert row.timestamp == 1784797260000
    assert row.subtask_ids == ["ENG-9"]
    assert row.attachments == [
        {
            "name": "spec.txt",
            "url": "https://uploads.linear.app/spec.txt",
            "mime": None,
            "size": None,
        }
    ]
    assert row.provider_data["team"] == {"id": "team-uuid-1", "key": "ENG"}
    assert row.provider_data["state"] == {
        "id": "state-backlog",
        "name": "Backlog",
        "type": "backlog",
    }
    assert row.provider_data["url"] == "https://linear.app/acme/issue/ENG-1"
    assert row.provider_data["subtasks"] == [{"key": "ENG-9", "title": "Подзадача"}]


def test_state_type_maps_to_tri_state_terminal_and_archived_is_unknown() -> None:
    completed = _issue(1)
    completed["state"] = {"id": "completed", "name": "Done", "type": "completed"}
    canceled = _issue(2)
    canceled["state"] = {"id": "canceled", "name": "Canceled", "type": "canceled"}
    active = _issue(3)
    absent = _issue(4)
    absent.pop("state")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_page([completed, canceled, active, absent], cursor=None),
        )

    rows = list(_board(handler).iter_raw("ENG", None))

    assert [row.terminal for row in rows] == [True, True, False, None]
    assert all(row.archived is None for row in rows)


def test_naive_and_offset_timestamps_are_treated_as_utc() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_page(
                [
                    _issue(1, updated="2026-07-23T09:01:00"),
                    _issue(2, updated="2026-07-23T12:01:00+03:00"),
                    _issue(3, updated="не дата"),
                    {key: value for key, value in _issue(4).items() if key != "updatedAt"},
                ],
                cursor=None,
            ),
        )

    rows = list(_board(handler).iter_raw("ENG", None))
    assert [row.timestamp for row in rows] == [1784797260000, 1784797260000, None, None]


def test_limit_stops_before_the_next_page_is_requested() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=_page([_issue(1), _issue(2)], cursor="cursor-1"),
        )

    assert [row.key for row in _board(handler).iter_raw("ENG", 1)] == ["ENG-1"]
    assert calls == 1


def test_fetch_one_shares_the_listing_mapper_and_accepts_identifier() -> None:
    variables: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if "issue(id:" in body["query"]:
            variables.append(body["variables"])
            return httpx.Response(200, json={"data": {"issue": _issue(1)}})
        return httpx.Response(200, json=_page([_issue(1)], cursor=None))

    board = _board(handler)
    raw = next(iter(board.iter_raw("ENG", 1)))
    one = board.fetch_one("ENG-1")

    assert one is not None
    assert dataclasses.asdict(one) == dataclasses.asdict(raw)
    assert variables == [{"id": "ENG-1"}]


def test_fetch_one_returns_none_on_entity_not_found() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "errors": [
                    {
                        "message": "Entity not found",
                        "extensions": {"code": "ENTITY_NOT_FOUND"},
                    }
                ]
            },
        )

    assert _board(handler).fetch_one("ENG-404") is None


def test_fetch_one_returns_none_when_issue_is_null() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"issue": None}})

    assert _board(handler).fetch_one("ENG-404") is None
