from __future__ import annotations
import time
from collections.abc import Callable


def _is_rate_limit(e: Exception) -> bool:
    name = type(e).__name__.lower()
    msg = str(e).lower()
    return (
        "ratelimit" in name
        or "rate limit" in msg
        or "rate_limit" in msg
        or "429" in msg
        or "too many requests" in msg
    )


def with_voyage_retry(fn: Callable, *, tries: int = 6, wait: float = 22.0, sleep=time.sleep):
    """Повторяет fn при rate-limit ошибках Voyage (free tier ~3 RPM).
    Не-rate-limit ошибки пробрасываются сразу. `sleep` инъектируется для тестов."""
    last: Exception | None = None
    for _ in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — фильтруем по типу ниже
            if not _is_rate_limit(e):
                raise
            last = e
            sleep(wait)
    raise last  # type: ignore[misc]
