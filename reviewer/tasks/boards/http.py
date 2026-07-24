"""Осторожный HTTP-транспорт для провайдеров досок."""
from __future__ import annotations

import time
from collections.abc import Callable, Collection
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx

from reviewer.tasks.boards.errors import BoardProviderError


class BoardHttpClient:
    """JSON-клиент с retry только для безопасных read-операций."""

    def __init__(
        self,
        client: Any,
        *,
        attempts: int = 3,
        backoff_base: float = 1.0,
        max_wait: float = 8.0,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        secrets: Collection[str] = (),
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least one")
        self._client = client
        self._attempts = attempts
        self._backoff_base = backoff_base
        self._max_wait = max_wait
        self._sleep = sleeper
        self._clock = clock
        self._secrets = frozenset(secrets)

    def request_json(
        self,
        method: str,
        path: str,
        *,
        operation: Literal["read", "write"],
        **kwargs: Any,
    ) -> Any:
        if operation not in {"read", "write"}:
            raise ValueError("operation must be 'read' or 'write'")
        for attempt in range(self._attempts):
            try:
                request = getattr(self._client, "request", None)
                if callable(request):
                    response = request(method, path, **kwargs)
                else:
                    response = getattr(self._client, method.lower())(path, **kwargs)
            except httpx.RequestError:
                if operation == "read" and attempt < self._attempts - 1:
                    self._sleep(self._wait_for(attempt, None))
                    continue
                raise BoardProviderError(
                    "transient",
                    "Board HTTP transport request failed.",
                    hint="Check the board network connection and try again.",
                    retryable=operation == "read",
                    secrets=self._secrets,
                ) from None

            status = response.status_code
            if 200 <= status < 300:
                if status == 204:
                    return {}
                try:
                    return response.json()
                except (TypeError, ValueError):
                    raise BoardProviderError(
                        "unsupported",
                        "Board HTTP response is not valid JSON.",
                        hint="Check the board API endpoint.",
                        secrets=self._secrets,
                    ) from None
            if (
                (status == 429 or 500 <= status < 600)
                and operation == "read"
                and attempt < self._attempts - 1
            ):
                self._sleep(self._wait_for(attempt, response.headers))
                continue
            raise self._error_for_status(status, operation)
        raise AssertionError("unreachable")

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def _wait_for(self, attempt: int, headers: Any) -> float:
        retry_after = None
        if headers is not None:
            retry_after = headers.get("Retry-After", headers.get("retry-after"))
        try:
            wait = float(retry_after) if retry_after is not None else self._backoff_base * 2**attempt
        except (TypeError, ValueError):
            try:
                wait = parsedate_to_datetime(retry_after).timestamp() - self._clock()
            except (TypeError, ValueError, IndexError, OverflowError):
                wait = self._backoff_base * 2**attempt
        return max(0.0, min(wait, self._max_wait))

    def _error_for_status(self, status: int, operation: str) -> BoardProviderError:
        if status == 401:
            category, hint = "authentication", "Check the board credentials."
        elif status == 403:
            category, hint = "permission", "Check the board permissions."
        elif status == 404:
            category, hint = "not_found", "Check the board endpoint or resource."
        elif status == 429:
            category, hint = "rate_limit", "Wait before trying the board again."
        elif 500 <= status < 600:
            category, hint = "transient", "The board is temporarily unavailable."
        else:
            category, hint = "unsupported", "Check the board API request."
        return BoardProviderError(
            category,
            f"Board HTTP request failed with status {status}.",
            hint=hint,
            retryable=operation == "read" and category in {"rate_limit", "transient"},
            secrets=self._secrets,
        )
