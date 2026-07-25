"""Модели пагинации досок: offset, номер страницы, курсор, Link-заголовок."""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from reviewer.tasks.boards.errors import BoardProviderError

_LINK_RE = re.compile(r'<(?P<url>[^>]+)>\s*;\s*rel="(?P<rel>[^"]+)"')


def _identity(payload: Any) -> list:
    return payload if isinstance(payload, list) else []


def _guard(pages: int, max_pages: int) -> None:
    """Страховка от зацикливания: обход не должен превышать max_pages страниц."""
    if pages >= max_pages:
        raise BoardProviderError(
            "unsupported",
            "Board pagination exceeded the maximum page count.",
            hint="Narrow the board query or raise the page size.",
        )


def paginate_offset(
    fetch: Callable[[int, int], Any],
    *,
    page_size: int,
    items: Callable[[Any], list] = _identity,
    max_pages: int = 1000,
) -> Iterator[Any]:
    """Страницы по offset/limit: обход до первой неполной страницы."""
    offset = 0
    for _ in range(max_pages):
        batch = items(fetch(offset, page_size))
        yield from batch
        if len(batch) < page_size:
            return
        offset += page_size
    _guard(max_pages, max_pages)


def paginate_page(
    fetch: Callable[[int, int], Any],
    *,
    page_size: int,
    items: Callable[[Any], list] = _identity,
    start: int = 1,
    max_pages: int = 1000,
) -> Iterator[Any]:
    """Страницы по номеру: обход до первой неполной страницы."""
    number = start
    for _ in range(max_pages):
        batch = items(fetch(number, page_size))
        yield from batch
        if len(batch) < page_size:
            return
        number += 1
    _guard(max_pages, max_pages)


def paginate_cursor(
    fetch: Callable[[str | None], Any],
    *,
    items: Callable[[Any], list],
    next_cursor: Callable[[Any], str | None],
    max_pages: int = 1000,
) -> Iterator[Any]:
    """Курсорная пагинация: обход, пока провайдер отдаёт следующий курсор."""
    cursor: str | None = None
    for _ in range(max_pages):
        payload = fetch(cursor)
        yield from items(payload)
        cursor = next_cursor(payload)
        if not cursor:
            return
    _guard(max_pages, max_pages)


def next_link(headers: Mapping[str, str]) -> str | None:
    """URL страницы rel="next" из заголовка Link; None — следующей страницы нет."""
    raw = headers.get("Link") or headers.get("link") or ""
    for match in _LINK_RE.finditer(raw):
        if match.group("rel") == "next":
            return match.group("url")
    return None


def paginate_link_header(
    fetch: Callable[[str | None], tuple[Any, Mapping[str, str]]],
    *,
    items: Callable[[Any], list] = _identity,
    max_pages: int = 1000,
) -> Iterator[Any]:
    """Пагинация по заголовку Link (модель GitHub): fetch отдаёт (payload, headers)."""
    url: str | None = None
    for _ in range(max_pages):
        payload, headers = fetch(url)
        yield from items(payload)
        url = next_link(headers)
        if not url:
            return
    _guard(max_pages, max_pages)
