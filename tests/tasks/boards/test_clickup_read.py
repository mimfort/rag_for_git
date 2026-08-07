"""Чтение ClickUp: personal-token без Bearer, страничная пагинация, маппинг RawTask."""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import httpx
import pytest

from reviewer.tasks.boards.clickup import ClickUpBoard
from reviewer.tasks.boards.errors import BoardProviderError

TOKEN = "pk_clickup_read_secret_value"
LIST_PATH = "/api/v2/list/901/task"


def _board(handler, **kwargs) -> ClickUpBoard:
    """ClickUpBoard на инжектированном MockTransport (без сети и без ожидания)."""
    params: dict = {
        "token": TOKEN,
        "list_id": "901",
        "key_prefix": "PRI",
        "key_pattern": r"PRI-\d+",
        "attachment_max_bytes": 1000,
        "attachment_timeout": 1.0,
        "attachment_store_chars": 1000,
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _: None,
    }
    params.update(kwargs)
    return ClickUpBoard(**params)


def _task(
    native: str,
    *,
    name: str = "Задача",
    updated: str = "1784797200000",
    custom_id: str | None = None,
    parent: str | None = None,
    markdown: str | None = None,
    description: str = "",
    status: str = "к выполнению",
    status_type: str = "custom",
    attachments: tuple[dict, ...] = (),
) -> dict:
    """Объект задачи ClickUp в форме ответа Get Tasks."""
    task: dict = {
        "id": native,
        "name": name,
        "description": description,
        "status": {"status": status, "type": status_type, "orderindex": 0, "color": "#ffffff"},
        "date_updated": updated,
        "url": f"https://app.clickup.com/t/{native}",
        "list": {"id": "901", "name": "Бэклог"},
    }
    if custom_id is not None:
        task["custom_id"] = custom_id
    if parent is not None:
        task["parent"] = parent
    if markdown is not None:
        task["markdown_description"] = markdown
    if attachments:
        task["attachments"] = list(attachments)
    return task


def test_personal_token_header_carries_no_bearer_prefix_and_page_starts_at_zero() -> None:
    requests: list[httpx.Request] = []
    pages = [
        {"tasks": [_task(f"a{number}") for number in range(100)]},
        {"tasks": [_task("b1")], "last_page": True},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=pages[len(requests) - 1])

    rows = list(_board(handler).iter_raw("PRI", None))

    assert len(rows) == 101
    assert [request.url.path for request in requests] == [LIST_PATH, LIST_PATH]
    assert requests[0].headers["Authorization"] == TOKEN
    assert dict(requests[0].url.params) == {
        "page": "0",
        "subtasks": "true",
        "include_closed": "true",
        "include_markdown_description": "true",
        "order_by": "updated",
    }
    assert dict(requests[1].url.params)["page"] == "1"


def test_last_page_flag_stops_pagination_without_an_extra_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"tasks": [_task(f"c{number}") for number in range(100)], "last_page": True},
        )

    rows = list(_board(handler).iter_raw("PRI", None))

    assert len(rows) == 100
    assert len(requests) == 1


def test_limit_stops_without_loading_the_next_page() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"tasks": [_task(f"d{number}") for number in range(100)]})

    rows = list(_board(handler).iter_raw("PRI", 2))

    assert len(rows) == 2
    assert calls == 1


def test_key_scheme_timestamp_and_subtask_grouping_inside_the_page() -> None:
    parent = _task("2kv", name="Родитель", markdown="## Родитель")
    child = _task("3ab", name="Ребёнок", parent="2kv", updated="1784797300000")
    custom = _task("4cd", custom_id="PRI-42", name="С кастомным ключом")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tasks": [parent, child, custom], "last_page": True})

    rows = list(_board(handler).iter_raw("PRI", None))

    assert [row.key for row in rows] == [
        f"PRI-{int('2kv', 36)}",
        f"PRI-{int('3ab', 36)}",
        "PRI-42",
    ]
    assert [row.board_id for row in rows] == ["2kv", "3ab", "4cd"]
    assert rows[0].project_code == rows[0].key
    assert rows[0].timestamp == 1784797200000
    assert rows[1].timestamp == 1784797300000
    assert rows[0].subtask_ids == ["3ab"]
    assert rows[0].provider_data["subtasks"] == [
        {"key": f"PRI-{int('3ab', 36)}", "title": "Ребёнок"}
    ]
    assert rows[1].provider_data["parent"] == "2kv"
    assert rows[0].archived is None
    assert rows[0].terminal is False


def test_status_type_maps_terminal_without_guessing_when_absent() -> None:
    closed = _task("2kv", status_type="closed")
    open_task = _task("3ab", status_type="custom")
    unknown = _task("4cd")
    unknown["status"].pop("type")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"tasks": [closed, open_task, unknown], "last_page": True},
        )

    rows = list(_board(handler).iter_raw("PRI", None))

    assert [row.terminal for row in rows] == [True, False, None]
    assert [row.archived for row in rows] == [None, None, None]


@pytest.mark.parametrize("updated", [None, "не дата", True])
def test_invalid_or_missing_update_timestamp_is_unknown(updated: object) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"tasks": [_task("2kv", updated=updated)], "last_page": True},
        )

    row = next(iter(_board(handler).iter_raw("PRI", None)))

    assert row.timestamp is None


def test_missing_key_prefix_falls_back_to_the_native_task_id() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tasks": [_task("2kv")], "last_page": True})

    rows = list(_board(handler, key_prefix="").iter_raw("PRI", None))

    assert [row.key for row in rows] == ["2kv"]


def test_updated_since_adds_date_updated_gt_in_milliseconds() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"tasks": []})

    list(_board(handler, updated_since_ms=1784797200000).iter_raw("PRI", None))

    assert seen[0]["date_updated_gt"] == "1784797200000"


def test_numeric_board_argument_overrides_the_configured_list_id() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"tasks": []})

    list(_board(handler).iter_raw("777", None))

    assert seen == ["/api/v2/list/777/task"]


def test_iter_raw_without_a_list_id_is_a_configuration_error() -> None:
    board = _board(lambda _request: httpx.Response(200, json={"tasks": []}), list_id="")
    with pytest.raises(BoardProviderError) as exc_info:
        list(board.iter_raw("PRI", None))
    assert exc_info.value.category == "configuration"
    board.close()


def test_fetch_one_shares_the_iter_mapper_and_missing_task_is_none() -> None:
    parent = _task("2kv", name="Родитель", markdown="## Родитель")
    child = _task("3ab", name="Ребёнок", parent="2kv")
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == LIST_PATH:
            return httpx.Response(200, json={"tasks": [parent, child], "last_page": True})
        if request.url.path == "/api/v2/task/2kv":
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={**parent, "subtasks": [child]})
        return httpx.Response(404, json={"err": "task not found"})

    board = _board(handler)
    raw = next(iter(board.iter_raw("PRI", 1)))
    one = board.fetch_one(raw.key)

    assert one is not None
    assert dataclasses.asdict(one) == dataclasses.asdict(raw)
    assert seen[0] == {"include_subtasks": "true", "include_markdown_description": "true"}
    assert board.fetch_one("PRI-999999999") is None
    board.close()


def test_fetch_one_uses_custom_task_ids_when_the_team_id_is_configured() -> None:
    task = _task("4cd", custom_id="PRI-42", markdown="## Кастомный ключ")
    seen: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/api/v2/task/PRI-42":
            return httpx.Response(200, json=task)
        return httpx.Response(404, json={"err": "task not found"})

    board = _board(handler, team_id="9007")
    one = board.fetch_one("PRI-42")

    assert one is not None
    assert one.board_id == "4cd"
    assert ("/api/v2/task/PRI-42", {
        "include_subtasks": "true",
        "include_markdown_description": "true",
        "custom_task_ids": "true",
        "team_id": "9007",
    }) in seen
    board.close()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.clickup.com/api/v2",
        "https://user@api.clickup.com/api/v2",
        "https://api.clickup.com/api/v2?token=leak",
        "https://api.clickup.com/api/v2#frag",
        "ftp://api.clickup.com/api/v2",
    ],
)
def test_api_base_must_be_an_https_url_without_userinfo(base_url: str) -> None:
    requests: list[httpx.Request] = []

    with pytest.raises(BoardProviderError) as exc_info:
        _board(
            lambda request: requests.append(request) or httpx.Response(200, json={}),
            base_url=base_url,
        )

    assert exc_info.value.category == "configuration"
    assert TOKEN not in repr(exc_info.value)
    assert requests == []


def test_rate_limited_read_waits_by_retry_after_and_reports_rate_limit() -> None:
    waits: list[float] = []
    board = _board(
        lambda _request: httpx.Response(
            429,
            headers={"Retry-After": "2"},
            json={"err": "rate limit reached"},
        ),
        sleeper=waits.append,
    )

    with pytest.raises(BoardProviderError) as exc_info:
        list(board.iter_raw("PRI", None))

    assert exc_info.value.category == "rate_limit"
    assert exc_info.value.retryable is True
    assert waits == [2.0, 2.0]
    board.close()


def test_rate_limited_read_falls_back_to_the_x_ratelimit_reset_header() -> None:
    waits: list[float] = []
    reset = datetime.now(UTC).timestamp() + 3

    board = _board(
        lambda _request: httpx.Response(
            429,
            headers={"X-RateLimit-Reset": str(reset)},
            json={"err": "rate limit reached"},
        ),
        sleeper=waits.append,
    )

    with pytest.raises(BoardProviderError) as exc_info:
        list(board.iter_raw("PRI", None))

    assert exc_info.value.category == "rate_limit"
    assert waits and 0.0 < waits[0] <= 8.0
    board.close()


def test_empty_token_is_rejected_before_any_request() -> None:
    requests: list[httpx.Request] = []

    with pytest.raises(BoardProviderError) as exc_info:
        _board(
            lambda request: requests.append(request) or httpx.Response(200, json={}),
            token="  ",
        )

    assert exc_info.value.category == "configuration"
    assert requests == []
