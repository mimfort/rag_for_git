"""Тесты общего REST-скелета адаптеров досок."""
from __future__ import annotations

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.restbase import RestBoardBase


def _board(handler, **kwargs) -> RestBoardBase:
    return RestBoardBase(
        base_url="https://board.test/api",
        secrets=("top-secret-token",),
        key_pattern=r"PRI-\d+",
        url_template="https://board.test/task/{code}",
        headers={"Authorization": "Bearer top-secret-token"},
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
        **kwargs,
    )


def test_read_goes_through_injected_transport_with_headers():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    board = _board(handler)
    assert board._read("GET", "/tasks") == {"ok": True}
    assert seen[0].url.path == "/api/tasks"
    assert seen[0].headers["Authorization"] == "Bearer top-secret-token"
    board.close()


def test_query_params_are_attached_to_every_request():
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={})

    board = _board(handler, params={"key": "app-key", "token": "top-secret-token"})
    board._read("GET", "/cards")
    board._write("PUT", "/cards/1", json={"desc": "x"})
    assert seen == [
        {"key": "app-key", "token": "top-secret-token"},
        {"key": "app-key", "token": "top-secret-token"},
    ]
    board.close()


def test_task_url_uses_template_and_tolerates_empty_template():
    board = _board(lambda request: httpx.Response(200, json={}))
    assert board._task_url("PRI-7") == "https://board.test/task/PRI-7"
    board.close()

    plain = RestBoardBase(base_url="https://board.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={})))
    assert plain._task_url("PRI-7") == ""
    plain.close()


def test_close_closes_underlying_transport():
    closed: list[bool] = []

    class _Transport(httpx.MockTransport):
        def close(self) -> None:
            closed.append(True)
            super().close()

    board = RestBoardBase(
        base_url="https://board.test",
        transport=_Transport(lambda request: httpx.Response(200, json={})),
    )
    board.close()
    assert closed == [True]


def test_secret_never_leaks_into_error_text():
    board = _board(lambda request: httpx.Response(403, json={"token": "top-secret-token"}))
    with pytest.raises(BoardProviderError) as exc_info:
        board._read("GET", "/tasks")
    assert exc_info.value.category == "permission"
    assert "top-secret-token" not in f"{exc_info.value!s}{exc_info.value!r}"
    board.close()


def test_write_is_not_retried_but_read_is():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(500, json={})

    board = _board(handler, attempts=2)
    with pytest.raises(BoardProviderError):
        board._read("GET", "/tasks")
    read_calls = len(calls)
    calls.clear()
    with pytest.raises(BoardProviderError):
        board._write("POST", "/tasks", json={})
    assert read_calls == 2
    assert len(calls) == 1
    board.close()
