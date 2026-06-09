from __future__ import annotations
import time
from collections.abc import Callable


def _is_transient(e: Exception) -> bool:
    """Определяет, является ли исключение транзиентной ошибкой LLM-провайдера.

    Покрывает: rate-limit (429), таймауты, сетевые обрывы, серверные ошибки (5xx/529).
    Проверяет как имя типа исключения, так и его текстовое сообщение."""
    name = type(e).__name__.lower()
    msg = str(e).lower()

    # Rate-limit / 429
    if ("ratelimit" in name or "rate_limit" in name
            or "rate limit" in msg or "rate_limit" in msg
            or "429" in msg or "too many requests" in msg):
        return True

    # Таймауты
    if "timeout" in name or "timeout" in msg or "timed out" in msg:
        return True

    # Сетевые ошибки
    if ("connection" in name
            or "connection reset" in msg or "connection error" in msg
            or ("connection" in msg and "error" in msg)):
        return True

    # Серверные ошибки 5xx / 529
    if ("internalservererror" in name or "badgateway" in name
            or "serviceunavailable" in name or "overloaded" in name
            or "500" in msg or "502" in msg or "503" in msg or "529" in msg
            or "internal server error" in msg or "bad gateway" in msg
            or "service unavailable" in msg or "overloaded" in msg):
        return True

    return False


def with_llm_retry(fn: Callable, *, tries: int = 4, base_wait: float = 2.0, sleep=time.sleep):
    """Повторяет fn при транзиентных ошибках LLM-провайдера (429/5xx/таймауты/сеть)
    с экспоненциальным backoff: base_wait * 2**attempt. Прочие ошибки — сразу наружу."""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — фильтруем по типу ниже
            if not _is_transient(e):
                raise
            last = e
            sleep(base_wait * (2 ** attempt))
    raise last  # type: ignore[misc]
