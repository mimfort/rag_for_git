"""Тесты для reviewer.llm._retry.with_llm_retry."""
import pytest

from reviewer.llm._retry import with_llm_retry


# ---------------------------------------------------------------------------
# Вспомогательные классы-исключения
# ---------------------------------------------------------------------------

class RateLimitError(Exception):
    """Имитирует openai.RateLimitError — имя типа содержит 'RateLimitError'."""


class InternalServerError(Exception):
    """Имитирует openai.InternalServerError."""


class APITimeoutError(Exception):
    """Имитирует openai.APITimeoutError."""


class APIConnectionError(Exception):
    """Имитирует openai.APIConnectionError."""


# ---------------------------------------------------------------------------
# Основные сценарии
# ---------------------------------------------------------------------------

def test_retry_on_rate_limit_then_success():
    """fn падает дважды с RateLimitError, затем возвращает значение.
    Результат получен, sleep вызван дважды с возрастающими интервалами."""
    calls = []
    sleep_args: list[float] = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise RateLimitError("Rate limit exceeded")
        return "ok"

    result = with_llm_retry(fn, base_wait=1.0, sleep=lambda s: sleep_args.append(s))
    assert result == "ok"
    assert len(calls) == 3
    assert len(sleep_args) == 2
    # Экспоненциальный backoff: base_wait * 2**0, base_wait * 2**1
    assert sleep_args[0] == pytest.approx(1.0)
    assert sleep_args[1] == pytest.approx(2.0)
    assert sleep_args[1] > sleep_args[0], "Интервалы должны возрастать"


def test_non_transient_error_propagates_immediately():
    """ValueError пробрасывается сразу без retry."""
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("логическая ошибка")

    with pytest.raises(ValueError, match="логическая ошибка"):
        with_llm_retry(fn, base_wait=0.0, sleep=lambda _: None)

    assert len(calls) == 1, "Retry не должен выполняться при не-транзиентных ошибках"


def test_exhausted_tries_raises_last_exception():
    """После исчерпания tries пробрасывается последнее исключение."""
    class RateLimitError2(Exception):
        pass

    def fn():
        raise RateLimitError2("rate_limit")

    with pytest.raises(RateLimitError2):
        with_llm_retry(fn, tries=3, base_wait=0.0, sleep=lambda _: None)


def test_retry_on_503_message():
    """Ошибка с текстом '503' считается транзиентной и ретраится."""
    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("HTTP 503 Service Unavailable")
        return "recovered"

    result = with_llm_retry(fn, base_wait=0.0, sleep=lambda _: None)
    assert result == "recovered"
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Дополнительные сценарии по именам типов openai
# ---------------------------------------------------------------------------

def test_retry_on_api_timeout_error():
    """APITimeoutError (имя содержит 'Timeout') ретраится."""
    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            raise APITimeoutError("request timed out")
        return "done"

    result = with_llm_retry(fn, base_wait=0.0, sleep=lambda _: None)
    assert result == "done"
    assert len(calls) == 2


def test_retry_on_api_connection_error():
    """APIConnectionError (имя содержит 'Connection') ретраится."""
    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            raise APIConnectionError("connection reset by peer")
        return "done"

    result = with_llm_retry(fn, base_wait=0.0, sleep=lambda _: None)
    assert result == "done"
    assert len(calls) == 2


def test_retry_on_internal_server_error():
    """InternalServerError (имя содержит 'InternalServer') ретраится."""
    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            raise InternalServerError("500 internal server error")
        return "done"

    result = with_llm_retry(fn, base_wait=0.0, sleep=lambda _: None)
    assert result == "done"
    assert len(calls) == 2


def test_first_call_success_no_sleep():
    """Если fn успешна с первого раза — sleep не вызывается."""
    sleep_calls = []

    def fn():
        return 42

    result = with_llm_retry(fn, sleep=lambda s: sleep_calls.append(s))
    assert result == 42
    assert sleep_calls == []


def test_tries_controls_max_attempts():
    """tries=2 означает ровно 2 попытки при постоянной транзиентной ошибке."""
    calls = []

    def fn():
        calls.append(1)
        raise RateLimitError("429")

    with pytest.raises(RateLimitError):
        with_llm_retry(fn, tries=2, base_wait=0.0, sleep=lambda _: None)

    assert len(calls) == 2
