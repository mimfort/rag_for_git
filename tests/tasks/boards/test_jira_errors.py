from __future__ import annotations

import logging

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from tests.tasks.boards.jira_helpers import board


@pytest.mark.parametrize(
    ("status", "category", "retryable", "attempts"),
    [
        (401, "authentication", False, 1),
        (403, "permission", False, 1),
        (404, "not_found", False, 1),
        (429, "rate_limit", True, 3),
        (503, "transient", True, 3),
    ],
)
def test_http_errors_are_classified_retried_and_secret_safe(
    status, category, retryable, attempts, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            headers={"Retry-After": "2"},
            json={
                "token": "jira-secret-token",
                "email": "bot@example.test",
                "Authorization": request.headers.get("Authorization"),
                "query": str(request.url.query),
            },
        )

    with caplog.at_level(logging.WARNING), pytest.raises(BoardProviderError) as exc_info:
        board(handler, sleeper=sleeps.append).validate_connection("PRI")

    error = exc_info.value
    assert (error.category, error.retryable, calls) == (category, retryable, attempts)
    assert sleeps == ([2.0, 2.0] if attempts == 3 else [])
    rendered = f"{error!s}\n{error!r}\n{caplog.text}"
    for secret in ("jira-secret-token", "bot@example.test", "Authorization", "query"):
        assert secret not in rendered


def test_write_is_never_retried_after_request_was_sent() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"secret": "body"})

    with pytest.raises(BoardProviderError):
        board(handler).create("# Новая", title="Новая", target=None, project="PRI")
    assert calls == 1
