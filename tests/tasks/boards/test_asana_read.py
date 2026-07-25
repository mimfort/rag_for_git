"""Чтение доски Asana: opaque-курсор пагинации, маппинг RawTask, limit, fetch_one."""
from __future__ import annotations

import dataclasses

import httpx
import pytest

from reviewer.tasks.boards.asana import AsanaBoard
from reviewer.tasks.boards.errors import BoardProviderError

BASE = "https://app.asana.test/api/1.0"
PROJECT_GID = "1201234567890"
SECRET = "asana-secret-token"


def _gid(number: int) -> str:
    """Длинный числовой gid, как у настоящей Asana."""
    return f"120765432{number:04d}"


def _task(number: int, **over: object) -> dict:
    task = {
        "gid": _gid(number),
        "name": f"Задача {number}",
        "html_notes": f"<body>Описание <em>{number}</em></body>",
        "notes": f"Описание {number}",
        "completed": False,
        "modified_at": "2026-07-23T09:12:00.000Z",
        "permalink_url": f"https://app.asana.test/0/{PROJECT_GID}/{_gid(number)}",
        "num_subtasks": 0,
        "memberships": [
            {
                "project": {"gid": PROJECT_GID},
                "section": {"gid": "5001", "name": "Todo"},
            }
        ],
    }
    task.update(over)
    return task


def _board(handler, **kwargs: object) -> AsanaBoard:
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
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _: None,
    }
    options.update(kwargs)
    return AsanaBoard(**options)  # type: ignore[arg-type]


def test_listing_sends_opt_fields_and_follows_opaque_offset_cursor() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params.get("offset") == "eyJ0eXAiOiJKV1Qi":
            return httpx.Response(200, json={"data": [_task(2)], "next_page": None})
        return httpx.Response(
            200,
            json={
                "data": [_task(1)],
                "next_page": {
                    "offset": "eyJ0eXAiOiJKV1Qi",
                    "path": "/tasks?offset=eyJ0eXAiOiJKV1Qi",
                    "uri": f"{BASE}/tasks?offset=eyJ0eXAiOiJKV1Qi",
                },
            },
        )

    board = _board(handler)
    rows = list(board.iter_raw("ASN", None))

    assert [row.key for row in rows] == [f"ASN-{_gid(1)}", f"ASN-{_gid(2)}"]
    assert [row.board_id for row in rows] == [_gid(1), _gid(2)]
    assert [row.project_code for row in rows] == [f"ASN-{_gid(1)}", f"ASN-{_gid(2)}"]
    assert len(requests) == 2
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/1.0/tasks"
    assert requests[0].headers["Authorization"] == f"Bearer {SECRET}"
    assert dict(requests[0].url.params) == {
        "project": PROJECT_GID,
        "limit": "100",
        "opt_fields": AsanaBoard._FIELDS,
    }
    assert dict(requests[1].url.params) == {
        "project": PROJECT_GID,
        "limit": "100",
        "opt_fields": AsanaBoard._FIELDS,
        "offset": "eyJ0eXAiOiJKV1Qi",
    }
    board.close()


def test_raw_task_fields_are_mapped_from_flat_listing_payload() -> None:
    board = _board(
        lambda _request: httpx.Response(
            200,
            json={
                "data": [
                    _task(
                        1,
                        completed=True,
                        num_subtasks=3,
                        modified_at="2026-07-24T18:30:45.123Z",
                    )
                ],
                "next_page": None,
            },
        )
    )
    row = next(iter(board.iter_raw("ASN", None)))

    assert row.title == "Задача 1"
    assert row.description == "<body>Описание <em>1</em></body>"
    assert row.status == "Todo"
    assert row.completed is True
    assert row.timestamp == 1784917845123
    assert row.subtask_ids == []
    assert row.provider_data["num_subtasks"] == 3
    assert row.provider_data["permalink_url"].endswith(_gid(1))
    assert row.provider_data["section"] == {"gid": "5001", "name": "Todo"}
    board.close()


def test_timestamp_is_epoch_ms_and_missing_modified_at_is_zero() -> None:
    board = _board(
        lambda _request: httpx.Response(
            200,
            json={"data": [_task(1), _task(2, modified_at=None)], "next_page": None},
        )
    )
    rows = list(board.iter_raw("ASN", None))

    assert rows[0].timestamp == 1784797920000
    assert rows[1].timestamp == 0
    board.close()


def test_limit_stops_without_loading_the_next_page() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "data": [_task(1), _task(2)],
                "next_page": {"offset": "more", "path": "/tasks", "uri": f"{BASE}/tasks"},
            },
        )

    board = _board(handler)
    assert [row.key for row in board.iter_raw("ASN", 1)] == [f"ASN-{_gid(1)}"]
    assert calls == 1
    board.close()


def test_numeric_board_argument_overrides_configured_project_gid() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("project"))
        return httpx.Response(200, json={"data": [], "next_page": None})

    board = _board(handler)
    list(board.iter_raw("777000111", None))
    assert seen == ["777000111"]
    board.close()


def test_missing_project_gid_is_a_configuration_error() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [], "next_page": None})

    board = _board(handler, project_gid="")
    with pytest.raises(BoardProviderError) as exc_info:
        list(board.iter_raw("ASN", None))
    assert exc_info.value.category == "configuration"
    assert SECRET not in f"{exc_info.value!s}{exc_info.value!r}"
    assert requests == []
    board.close()


def test_key_without_prefix_falls_back_to_bare_gid() -> None:
    board = _board(
        lambda _request: httpx.Response(200, json={"data": [_task(1)], "next_page": None}),
        key_prefix="",
    )
    row = next(iter(board.iter_raw("ASN", None)))
    assert row.key == _gid(1)
    assert row.board_id == _gid(1)
    board.close()


def test_fetch_one_shares_the_mapper_and_maps_404_to_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/1.0/tasks/{_gid(9)}":
            return httpx.Response(404, json={"token": SECRET})
        if request.url.path == f"/api/1.0/tasks/{_gid(1)}":
            return httpx.Response(200, json={"data": _task(1)})
        return httpx.Response(200, json={"data": [_task(1)], "next_page": None})

    board = _board(handler)
    raw = next(iter(board.iter_raw("ASN", 1)))
    one = board.fetch_one(f"ASN-{_gid(1)}")

    assert one is not None
    assert dataclasses.asdict(one) == dataclasses.asdict(raw)
    assert board.fetch_one(f"ASN-{_gid(9)}") is None
    assert board.fetch_one("ASN-not-a-gid") is None
    board.close()


def test_fetch_one_requests_the_same_opt_fields_as_the_listing() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": _task(1)})

    board = _board(handler)
    board.fetch_one(f"ASN-{_gid(1)}")
    assert requests[0].url.path == f"/api/1.0/tasks/{_gid(1)}"
    assert dict(requests[0].url.params) == {"opt_fields": AsanaBoard._FIELDS}
    board.close()
