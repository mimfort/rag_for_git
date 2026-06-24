from __future__ import annotations
import re
import time

import httpx

# Маркер идемпотентности в теле комментария (общий для всех VCS-провайдеров).
_FP = re.compile(r"<!-- ai-review:([0-9a-f]+) -->")
_RETRY_CODES = {429, 502, 503, 504}


class _RetryTransport:
    """Обёртка над httpx-транспортом с retry по статусам 429/502/503/504."""

    def __init__(
        self,
        wrapped,
        *,
        attempts: int = 3,
        backoff_base: float = 1.0,
        max_wait: float = 8.0,
        _sleep=time.sleep,
    ):
        self._wrapped = wrapped
        self._attempts = attempts
        self._backoff_base = backoff_base
        self._max_wait = max_wait
        self._sleep = _sleep

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in range(self._attempts):
            response = self._wrapped.handle_request(request)
            if response.status_code not in _RETRY_CODES:
                return response
            if attempt < self._attempts - 1:
                retry_after = response.headers.get("retry-after")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = self._backoff_base * (2 ** attempt)
                else:
                    wait = self._backoff_base * (2 ** attempt)
                # max(0, …): невалидный/отрицательный Retry-After (напр. "-1")
                # не должен уводить sleep в минус — time.sleep(<0) бросает ValueError.
                self._sleep(max(0.0, min(wait, self._max_wait)))
        assert response is not None
        return response

    def close(self) -> None:
        self._wrapped.close()

    def __enter__(self):
        self._wrapped.__enter__()
        return self

    def __exit__(self, *args):
        return self._wrapped.__exit__(*args)
