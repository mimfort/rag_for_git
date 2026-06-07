import pytest

from reviewer.index._retry import with_voyage_retry


class RateLimitError(Exception):
    pass


def test_retries_on_rate_limit_then_succeeds():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimitError("429 rate limit exceeded")
        return "ok"

    slept: list[float] = []
    out = with_voyage_retry(fn, tries=5, wait=1.0, sleep=lambda w: slept.append(w))
    assert out == "ok"
    assert calls["n"] == 3
    assert slept == [1.0, 1.0]   # два ожидания перед третьей попыткой


def test_non_rate_limit_error_propagates_immediately():
    def fn():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        with_voyage_retry(fn, sleep=lambda w: None)


def test_gives_up_after_tries_and_reraises_last():
    def fn():
        raise RateLimitError("rate limit")

    with pytest.raises(RateLimitError):
        with_voyage_retry(fn, tries=2, wait=0.0, sleep=lambda w: None)
