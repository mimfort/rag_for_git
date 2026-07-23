from __future__ import annotations

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


def test_read_retries_rate_limit_using_bounded_retry_after():
    client = _Client([_Response(429, headers={"Retry-After": "99"}), _Response(200, {"id": 1})])
    sleeps = []
    http = BoardHttpClient(client, attempts=3, max_wait=2.0, sleeper=sleeps.append)

    assert http.request_json("GET", "/issues?token=secret", operation="read") == {"id": 1}
    assert len(client.calls) == 2
    assert sleeps == [2.0]


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
