"""Чтение карточек Trello: пагинация по ``before``, маппинг RawTask, limit, время."""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import httpx
import pytest

from reviewer.config.task_board import TaskSyncFilter
from reviewer.tasks.boards.base import TaskListing
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.trello import TrelloBoard

API_KEY = "trello-app-key"
TOKEN = "trello-secret-token"
BOARD = "5f00000000000000000000b0"
BACKLOG = "5f00000000000000000000l1"
DONE = "5f00000000000000000000l2"

LISTS = [{"id": BACKLOG, "name": "Backlog"}, {"id": DONE, "name": "Done"}]


def _card(number: int) -> dict:
    """Карточка Trello: id монотонен по времени создания, активность — обратна номеру."""
    return {
        "id": f"{number:024x}",
        "idShort": number,
        "name": f"Задача {number}",
        "desc": f"## Проблема\n\nОписание TRL-{number}",
        "dateLastActivity": f"2026-07-23T09:{(60 - number) % 60:02d}:00.000Z",
        "idList": BACKLOG if number > 1 else DONE,
        "idBoard": BOARD,
        "shortLink": f"sh{number:06d}",
        "shortUrl": f"https://trello.com/c/sh{number:06d}",
        "closed": False,
    }


def _board(handler, **kwargs) -> TrelloBoard:
    return TrelloBoard(
        api_key=API_KEY,
        api_token=TOKEN,
        board_id=kwargs.pop("board_id", BOARD),
        key_prefix=kwargs.pop("key_prefix", "TRL"),
        key_pattern=r"TRL-\d+",
        url_template="https://trello.test/task/{code}",
        page_size=kwargs.pop("page_size", 2),
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
        **kwargs,
    )


def _paged_handler(cards: list[dict], calls: list[httpx.Request]):
    """Фейк листинга: ``/cards`` открыт только для активных, ``/cards/all`` — для всех."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/lists"):
            return httpx.Response(200, json=LISTS)
        all_path = f"/1/boards/{BOARD}/cards/all"
        open_path = f"/1/boards/{BOARD}/cards"
        if request.url.path == all_path:
            available = cards
        elif request.url.path == open_path:
            available = [card for card in cards if card.get("closed") is not True]
        else:
            return httpx.Response(404, json={})
        limit = int(request.url.params.get("limit", "1000"))
        before = request.url.params.get("before")
        rows = sorted(available, key=lambda item: item["id"], reverse=True)
        if before:
            rows = [item for item in rows if item["id"] < before]
        return httpx.Response(200, json=rows[:limit])

    return handler


def test_cards_are_paged_by_before_cursor_with_exact_params() -> None:
    calls: list[httpx.Request] = []
    listing = _board(_paged_handler([_card(n) for n in range(1, 6)], calls)).iter_raw(
        "TRL",
        None,
        sync_filter=TaskSyncFilter(max_age_days=30, include_archived=False),
        now_ms=123,
    )
    rows = list(listing)

    assert isinstance(listing, TaskListing)
    assert [row.key for row in rows] == ["TRL-1", "TRL-2", "TRL-3", "TRL-4", "TRL-5"]
    assert [(call.method, call.url.path) for call in calls] == [
        ("GET", f"/1/boards/{BOARD}/lists"),
        ("GET", f"/1/boards/{BOARD}/cards/all"),
        ("GET", f"/1/boards/{BOARD}/cards/all"),
        ("GET", f"/1/boards/{BOARD}/cards/all"),
    ]
    assert dict(calls[0].url.params) == {"key": API_KEY, "token": TOKEN, "fields": "id,name"}
    assert dict(calls[1].url.params) == {
        "key": API_KEY,
        "token": TOKEN,
        "fields": TrelloBoard.CARD_FIELDS,
        "limit": "2",
    }
    assert calls[2].url.params.get("before") == f"{4:024x}"
    assert calls[3].url.params.get("before") == f"{2:024x}"
    assert listing.stats.filtered_by_age == 0
    assert listing.stats.filtered_archived == 0
    assert listing.stats.warnings == []


def test_unlimited_listing_uses_all_endpoint_and_includes_archived_cards() -> None:
    calls: list[httpx.Request] = []
    active = _card(1)
    archived = _card(2)
    archived["closed"] = True

    rows = list(_board(_paged_handler([active, archived], calls)).iter_raw("TRL", None))

    assert [row.key for row in rows] == ["TRL-1", "TRL-2"]
    assert [row.archived for row in rows] == [False, True]
    listing_paths = [call.url.path for call in calls if "/cards" in call.url.path]
    assert listing_paths == [f"/1/boards/{BOARD}/cards/all"] * 2


def test_rows_are_sorted_by_last_activity_and_limit_caps_output() -> None:
    calls: list[httpx.Request] = []
    cards = [_card(n) for n in range(1, 6)]
    cards[0]["closed"] = True
    board = _board(_paged_handler(cards, calls))

    limited = list(board.iter_raw("TRL", 2))
    assert [row.key for row in limited] == ["TRL-1", "TRL-2"]
    assert limited[0].archived is True
    assert [row.timestamp for row in board.iter_raw("TRL", None)] == sorted(
        (row.timestamp for row in board.iter_raw("TRL", None)), reverse=True
    )


def test_raw_task_maps_native_identifiers_status_and_epoch_ms() -> None:
    calls: list[httpx.Request] = []
    row = next(iter(_board(_paged_handler([_card(3)], calls)).iter_raw("TRL", None)))

    assert row.key == "TRL-3"
    assert row.project_code == "TRL-3"
    assert row.board_id == f"{3:024x}"
    assert row.status == "Backlog"
    assert row.title == "Задача 3"
    assert row.description == "## Проблема\n\nОписание TRL-3"
    assert row.timestamp == int(datetime(2026, 7, 23, 9, 57, tzinfo=UTC).timestamp() * 1000)
    assert row.archived is False
    assert row.terminal is None
    assert row.provider_data["short_link"] == "sh000003"
    assert row.subtask_ids == [] and row.attachments == []


def test_nullable_timestamp_sorting_and_native_closed_tri_state() -> None:
    calls: list[httpx.Request] = []
    archived = _card(1)
    archived["closed"] = True
    archived["dateLastActivity"] = "invalid"
    active = _card(2)
    active["closed"] = False
    active.pop("dateLastActivity")
    unknown = _card(3)
    unknown.pop("closed")

    rows = list(
        _board(_paged_handler([archived, active, unknown], calls)).iter_raw("TRL", None)
    )
    by_key = {row.key: row for row in rows}

    assert [row.key for row in rows] == ["TRL-3", "TRL-1", "TRL-2"]
    assert by_key["TRL-1"].timestamp is None
    assert by_key["TRL-2"].timestamp is None
    assert by_key["TRL-1"].archived is True
    assert by_key["TRL-2"].archived is False
    assert by_key["TRL-3"].archived is None
    assert all(row.terminal is None for row in rows)
    assert by_key["TRL-3"].provider_data["closed"] is None


def test_fetch_one_resolves_short_key_through_board_nested_endpoint() -> None:
    calls: list[httpx.Request] = []
    paged = _paged_handler([_card(3)], calls)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/1/boards/{BOARD}/cards/3":
            calls.append(request)
            return httpx.Response(200, json=_card(3))
        if request.url.path == f"/1/boards/{BOARD}/cards/404":
            calls.append(request)
            return httpx.Response(404, json={"token": TOKEN})
        return paged(request)

    board = _board(handler)
    raw = next(iter(board.iter_raw("TRL", 1)))
    one = board.fetch_one("TRL-3")

    assert one is not None
    assert dataclasses.asdict(one) == dataclasses.asdict(raw)
    assert board.fetch_one("TRL-404") is None
    detail = [call for call in calls if call.url.path.endswith("/cards/3")][0]
    assert detail.url.params.get("fields") == TrelloBoard.CARD_FIELDS


def test_native_card_id_key_is_read_through_the_cards_endpoint() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/lists"):
            return httpx.Response(200, json=LISTS)
        return httpx.Response(200, json=_card(3))

    one = _board(handler).fetch_one("sh000003")

    assert one is not None and one.key == "TRL-3"
    assert [call.url.path for call in calls if "cards" in call.url.path] == ["/1/cards/sh000003"]


def test_board_id_is_required_and_project_scope_may_carry_it() -> None:
    calls: list[httpx.Request] = []
    handler = _paged_handler([_card(1)], calls)

    with pytest.raises(BoardProviderError) as exc_info:
        list(_board(handler, board_id="").iter_raw("TRL", None))
    assert exc_info.value.category == "configuration"
    assert calls == []

    rows = list(_board(handler, board_id="").iter_raw(BOARD, None))
    assert [row.key for row in rows] == ["TRL-1"]


def test_key_prefix_is_required_for_synthesised_keys() -> None:
    calls: list[httpx.Request] = []

    with pytest.raises(BoardProviderError) as exc_info:
        list(_board(_paged_handler([_card(1)], calls), key_prefix="").iter_raw("TRL", None))
    assert exc_info.value.category == "configuration"


def test_rate_limited_read_waits_the_interval_reported_by_trello() -> None:
    waits: list[float] = []
    responses = [
        httpx.Response(
            429,
            json={"token": TOKEN},
            headers={"x-rate-limit-api-token-interval-ms": "2500"},
        ),
        httpx.Response(200, json=LISTS),
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    board = TrelloBoard(
        api_key=API_KEY,
        api_token=TOKEN,
        board_id=BOARD,
        key_prefix="TRL",
        transport=httpx.MockTransport(handler),
        sleeper=waits.append,
    )
    assert board.list_targets("TRL")["targets"][0]["label"] == "Backlog"
    assert waits == [2.5]
    board.close()


def test_secret_stays_out_of_error_text_and_repr() -> None:
    board = _board(lambda _: httpx.Response(403, json={"token": TOKEN, "key": API_KEY}))
    with pytest.raises(BoardProviderError) as exc_info:
        board.validate_connection("TRL")
    text = f"{exc_info.value!s}{exc_info.value!r}"
    assert exc_info.value.category == "permission"
    assert TOKEN not in text and API_KEY not in text
    board.close()
