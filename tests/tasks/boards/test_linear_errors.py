"""Семантика ошибок Linear: validate_connection, GraphQL-коды, rate limit как HTTP 400."""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.linear import LinearBoard

SECRET = "linear-errors-secret"

TEAM = {
    "id": "team-uuid-eng",
    "key": "ENG",
    "name": "Команда ENG",
    "states": {
        "nodes": [
            {"id": "state-backlog", "name": "Backlog", "type": "backlog", "position": 0},
            {"id": "state-done", "name": "Done", "type": "completed", "position": 1},
        ]
    },
}


def _board(handler, **kwargs) -> LinearBoard:
    return LinearBoard(
        api_key=SECRET,
        api_base="https://api.linear.app",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
        **kwargs,
    )


def _ok_handler(teams: list[dict] | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if "teams(" in body["query"]:
            return httpx.Response(
                200,
                json={"data": {"teams": {"nodes": [TEAM] if teams is None else teams}}},
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "viewer": {"id": "user-1", "name": "robot", "displayName": "Робот"},
                    "organization": {"id": "org-1", "name": "Acme", "urlKey": "acme"},
                }
            },
        )

    return handle


def test_validate_connection_reports_identity_and_capabilities() -> None:
    result = _board(_ok_handler()).validate_connection("ENG")

    assert set(result) == {"status", "identity", "project", "capabilities", "warnings"}
    assert result["status"] == "ok"
    assert result["project"] == "ENG"
    assert result["identity"]["display_name"] == "Робот"
    assert result["identity"]["organization"] == "acme"
    assert result["capabilities"] == {"read": True, "create": True, "finish": True}
    assert result["warnings"] == []
    assert SECRET not in repr(result)


def test_validate_connection_warns_about_unknown_team() -> None:
    result = _board(_ok_handler(teams=[])).validate_connection("ZZZ")

    assert result["status"] == "ok"
    assert result["capabilities"] == {"read": True, "create": False, "finish": False}
    assert result["warnings"]


def test_validate_connection_warns_when_team_has_no_terminal_state() -> None:
    team = dict(TEAM)
    team["states"] = {
        "nodes": [{"id": "state-backlog", "name": "Backlog", "type": "backlog", "position": 0}]
    }
    result = _board(_ok_handler(teams=[team])).validate_connection("ENG")

    assert result["capabilities"]["finish"] is False
    assert result["warnings"]


@pytest.mark.parametrize(
    ("status", "category", "retryable", "attempts"),
    [
        (401, "authentication", False, 1),
        (403, "permission", False, 1),
        (404, "not_found", False, 1),
        (400, "unsupported", False, 1),
        (429, "rate_limit", True, 3),
        (503, "transient", True, 3),
    ],
)
def test_http_statuses_are_classified_and_secret_safe(
    status: int,
    category: str,
    retryable: bool,
    attempts: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            json={"token": SECRET, "Authorization": request.headers.get("Authorization")},
        )

    with caplog.at_level(logging.WARNING), pytest.raises(BoardProviderError) as exc_info:
        _board(handler).validate_connection("ENG")

    error = exc_info.value
    assert (error.category, error.retryable, calls) == (category, retryable, attempts)
    assert SECRET not in f"{error!s}\n{error!r}\n{caplog.text}"


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
def test_graphql_error_codes_are_not_swallowed(code: str, category: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"errors": [{"message": SECRET, "extensions": {"code": code}}]},
        )

    with pytest.raises(BoardProviderError) as exc_info:
        _board(handler).validate_connection("ENG")

    assert exc_info.value.category == category
    assert SECRET not in f"{exc_info.value!s}{exc_info.value!r}"


def test_rate_limit_delivered_as_http_400_is_classified_and_retried() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            400,
            headers={
                "X-RateLimit-Requests-Limit": "5000",
                "X-RateLimit-Requests-Remaining": "0",
                "X-RateLimit-Requests-Reset": "1784797260000",
            },
            json={"errors": [{"message": "rate", "extensions": {"code": "RATELIMITED"}}]},
        )

    board = LinearBoard(
        api_key=SECRET,
        api_base="https://api.linear.app",
        transport=httpx.MockTransport(handler),
        sleeper=sleeps.append,
    )
    with pytest.raises(BoardProviderError) as exc_info:
        board.validate_connection("ENG")

    assert exc_info.value.category == "rate_limit"
    assert exc_info.value.retryable is True
    assert (calls, len(sleeps)) == (3, 2)


def test_complexity_rate_limit_headers_are_recognized() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={
                "X-RateLimit-Complexity-Remaining": "0",
                "X-RateLimit-Complexity-Reset": "not-a-number",
            },
            json={"errors": [{"message": "complexity", "extensions": {"code": "RATELIMITED"}}]},
        )

    with pytest.raises(BoardProviderError) as exc_info:
        _board(handler).validate_connection("ENG")

    assert exc_info.value.category == "rate_limit"


def test_plain_http_400_stays_unsupported_without_rate_limit_headers() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={"X-RateLimit-Requests-Remaining": "4999"},
            json={"errors": [{"message": "bad query"}]},
        )

    with pytest.raises(BoardProviderError) as exc_info:
        _board(handler).validate_connection("ENG")

    assert exc_info.value.category == "unsupported"


def test_write_operations_are_never_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content.decode())
        if "teams(" in body["query"]:
            return httpx.Response(200, json={"data": {"teams": {"nodes": [TEAM]}}})
        return httpx.Response(503, json={"token": SECRET})

    with pytest.raises(BoardProviderError):
        _board(handler).create("# Новая", title="Новая", target=None, project="ENG")

    assert calls == 2


def test_transport_failure_is_transient_and_secret_safe() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connect to {SECRET} failed")

    with pytest.raises(BoardProviderError) as exc_info:
        _board(handler).validate_connection("ENG")

    assert exc_info.value.category == "transient"
    assert SECRET not in f"{exc_info.value!s}{exc_info.value!r}"
