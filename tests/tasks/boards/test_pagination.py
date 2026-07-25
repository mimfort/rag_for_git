"""Тесты генераторов пагинации провайдеров досок."""
from __future__ import annotations

import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.pagination import (
    next_link,
    paginate_cursor,
    paginate_link_header,
    paginate_offset,
    paginate_page,
)


def test_paginate_offset_walks_until_short_page():
    calls: list[tuple[int, int]] = []

    def fetch(offset: int, limit: int):
        calls.append((offset, limit))
        return [{"i": offset + n} for n in range(limit if offset < 4 else 1)]

    rows = list(paginate_offset(fetch, page_size=2))
    assert [row["i"] for row in rows] == [0, 1, 2, 3, 4]
    assert calls == [(0, 2), (2, 2), (4, 2)]


def test_paginate_offset_stops_on_empty_first_page():
    rows = list(paginate_offset(lambda offset, limit: [], page_size=50))
    assert rows == []


def test_paginate_page_starts_at_one_and_unwraps_items():
    calls: list[int] = []

    def fetch(page: int, size: int):
        calls.append(page)
        return {"data": [{"n": page}] * (size if page < 3 else 0)}

    rows = list(paginate_page(fetch, page_size=2, items=lambda payload: payload["data"]))
    assert len(rows) == 4
    assert calls == [1, 2, 3]


def test_paginate_cursor_follows_cursor_until_none():
    pages = {
        None: {"items": [1, 2], "next": "c1"},
        "c1": {"items": [3], "next": "c2"},
        "c2": {"items": [4], "next": None},
    }
    seen: list[str | None] = []

    def fetch(cursor: str | None):
        seen.append(cursor)
        return pages[cursor]

    rows = list(
        paginate_cursor(
            fetch,
            items=lambda payload: payload["items"],
            next_cursor=lambda payload: payload["next"],
        )
    )
    assert rows == [1, 2, 3, 4]
    assert seen == [None, "c1", "c2"]


def test_next_link_parses_rel_next_and_ignores_others():
    headers = {
        "Link": '<https://api.test/issues?page=2>; rel="next", '
        '<https://api.test/issues?page=9>; rel="last"'
    }
    assert next_link(headers) == "https://api.test/issues?page=2"
    assert next_link({"Link": '<https://api.test/issues?page=9>; rel="last"'}) is None
    assert next_link({}) is None


def test_paginate_link_header_follows_next_until_absent():
    pages = {
        None: ([1, 2], {"Link": '<https://api.test/issues?page=2>; rel="next"'}),
        "https://api.test/issues?page=2": ([3], {}),
    }
    seen: list[str | None] = []

    def fetch(url: str | None):
        seen.append(url)
        return pages[url]

    rows = list(paginate_link_header(fetch))
    assert rows == [1, 2, 3]
    assert seen == [None, "https://api.test/issues?page=2"]


def test_max_pages_guard_raises_instead_of_looping_forever():
    with pytest.raises(BoardProviderError) as exc_info:
        list(
            paginate_cursor(
                lambda cursor: {"items": [1], "next": "same"},
                items=lambda payload: payload["items"],
                next_cursor=lambda payload: payload["next"],
                max_pages=3,
            )
        )
    assert exc_info.value.category == "unsupported"
