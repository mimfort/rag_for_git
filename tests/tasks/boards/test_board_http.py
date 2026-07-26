from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.http import BoardHttpClient


class _Response:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.headers = headers or {}

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        value = next(self.responses)
        if isinstance(value, BaseException):
            raise value
        return value


class _SequenceClient:
    """Клиент-заглушка: отдаёт заранее заданную последовательность ответов."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self._responses:
            raise AssertionError("unexpected extra request")
        return self._responses.pop(0)


def _client_returning(responses: list[httpx.Response]) -> _SequenceClient:
    return _SequenceClient(responses)


def test_read_retries_rate_limit_using_bounded_retry_after():
    client = _Client([_Response(429, headers={"Retry-After": "99"}), _Response(200, {"id": 1})])
    sleeps = []
    http = BoardHttpClient(client, attempts=3, max_wait=2.0, sleeper=sleeps.append)

    assert http.request_json("GET", "/issues?token=secret", operation="read") == {"id": 1}
    assert len(client.calls) == 2
    assert sleeps == [2.0]


@pytest.mark.parametrize(
    ("retry_after", "max_wait", "expected_wait"),
    [
        (datetime(2030, 1, 1, tzinfo=UTC) + timedelta(seconds=10), 2.0, 2.0),
        (datetime(2030, 1, 1, tzinfo=UTC) - timedelta(seconds=10), 8.0, 0.0),
    ],
)
def test_read_retries_using_http_date_retry_after(retry_after, max_wait, expected_wait):
    now = datetime(2030, 1, 1, tzinfo=UTC)
    client = _Client([
        _Response(429, headers={"Retry-After": format_datetime(retry_after, usegmt=True)}),
        _Response(200, {"id": 1}),
    ])
    sleeps = []
    http = BoardHttpClient(
        client,
        attempts=2,
        max_wait=max_wait,
        sleeper=sleeps.append,
        clock=lambda: now.timestamp(),
    )

    assert http.request_json("GET", "/issues", operation="read") == {"id": 1}
    assert sleeps == [expected_wait]


def test_read_retries_any_transient_server_error():
    client = _Client([_Response(599), _Response(200, {"id": 1})])
    http = BoardHttpClient(client, attempts=2, sleeper=lambda _: None)

    assert http.request_json("GET", "/issues", operation="read") == {"id": 1}
    assert len(client.calls) == 2


def test_read_retries_transient_transport_failure_with_bounded_attempts():
    client = _Client([httpx.ConnectError("token=secret"), httpx.ConnectError("token=secret")])
    http = BoardHttpClient(client, attempts=2, sleeper=lambda _: None, secrets={"secret"})

    with pytest.raises(BoardProviderError, match="transport") as raised:
        http.request_json("GET", "/issues", operation="read")

    assert raised.value.category == "transient"
    assert raised.value.retryable is True
    assert "secret" not in str(raised.value)
    assert len(client.calls) == 2


@pytest.mark.parametrize("status, category", [(401, "authentication"), (403, "permission")])
def test_authentication_and_permission_responses_are_not_retried(status, category):
    client = _Client([_Response(status), _Response(200)])
    http = BoardHttpClient(client, attempts=3, sleeper=lambda _: None)

    with pytest.raises(BoardProviderError) as raised:
        http.request_json("GET", "/issues", operation="read")

    assert raised.value.category == category
    assert len(client.calls) == 1


@pytest.mark.parametrize("response", [_Response(503), httpx.ConnectError("network")])
def test_write_is_never_automatically_retried_after_uncertainty(response):
    client = _Client([response, _Response(200)])
    http = BoardHttpClient(client, attempts=3, sleeper=lambda _: None)

    with pytest.raises(BoardProviderError):
        http.request_json("POST", "/issues", operation="write")

    assert len(client.calls) == 1


def test_rate_limit_hint_turns_forbidden_into_retryable_rate_limit():
    responses = [
        httpx.Response(403, headers={"X-RateLimit-Remaining": "0"}, json={}),
        httpx.Response(200, json={"ok": True}),
    ]
    client = _client_returning(responses)
    sleeps: list[float] = []
    http = BoardHttpClient(
        client,
        attempts=3,
        max_wait=5.0,
        sleeper=sleeps.append,
        rate_limit_hint=lambda status, headers: (
            2.0 if status == 403 and headers.get("X-RateLimit-Remaining") == "0" else None
        ),
    )

    assert http.request_json("GET", "/issues", operation="read") == {"ok": True}
    assert sleeps == [2.0]


def test_rate_limit_hint_result_is_bounded_by_max_wait():
    client = _client_returning([httpx.Response(403, json={}), httpx.Response(200, json={})])
    sleeps: list[float] = []
    http = BoardHttpClient(
        client,
        attempts=2,
        max_wait=1.5,
        sleeper=sleeps.append,
        rate_limit_hint=lambda status, headers: 900.0,
    )

    http.request_json("GET", "/issues", operation="read")
    assert sleeps == [1.5]


def test_rate_limit_hint_error_category_is_rate_limit_when_attempts_exhausted():
    client = _client_returning([httpx.Response(403, json={})])
    http = BoardHttpClient(
        client,
        attempts=1,
        sleeper=lambda _: None,
        rate_limit_hint=lambda status, headers: 1.0,
    )

    with pytest.raises(BoardProviderError) as exc_info:
        http.request_json("GET", "/issues", operation="read")
    assert exc_info.value.category == "rate_limit"


def test_without_hint_forbidden_stays_permission_and_is_not_retried():
    client = _client_returning([httpx.Response(403, json={}), httpx.Response(200, json={})])
    sleeps: list[float] = []
    http = BoardHttpClient(client, attempts=3, sleeper=sleeps.append)

    with pytest.raises(BoardProviderError) as exc_info:
        http.request_json("GET", "/issues", operation="read")
    assert exc_info.value.category == "permission"
    assert sleeps == []


def test_rate_limit_hint_is_not_consulted_for_write_retry():
    client = _client_returning([httpx.Response(403, json={}), httpx.Response(200, json={})])
    sleeps: list[float] = []
    http = BoardHttpClient(
        client,
        attempts=3,
        sleeper=sleeps.append,
        rate_limit_hint=lambda status, headers: 1.0,
    )

    with pytest.raises(BoardProviderError) as exc_info:
        http.request_json("POST", "/issues", operation="write")
    assert exc_info.value.category == "rate_limit"
    assert sleeps == []
