"""Классификатор сбоя фетчера коммиченного слоя (PRI-234).

Ключевой инвариант: решение принимается по ФОРМЕ исключения (атрибут
response.status_code, имена классов в MRO), а не по его тексту — там живут
URL и токен VCS-клиента.
"""
from reviewer.config.fetch_errors import classify_fetch_error


class _Response:
    def __init__(self, status_code) -> None:
        self.status_code = status_code


class _HTTPStatusError(Exception):
    """Форма httpx.HTTPStatusError: есть .response и .request с секретами."""

    def __init__(self, message: str, status_code) -> None:
        super().__init__(message)
        self.response = _Response(status_code)


class _ConnectTimeout(Exception):
    pass


class _ConnectError(Exception):
    pass


class _NetworkError(Exception):
    pass


def test_http_status_is_extracted_from_response():
    assert classify_fetch_error(_HTTPStatusError("boom", 404)) == ("http", 404)
    assert classify_fetch_error(_HTTPStatusError("boom", 401)) == ("http", 401)


def test_timeout_wins_over_connection_in_class_name():
    assert classify_fetch_error(_ConnectTimeout("boom")) == ("timeout", None)


def test_connection_error_is_classified_by_class_name():
    assert classify_fetch_error(_ConnectError("boom")) == ("connection", None)
    assert classify_fetch_error(ConnectionError("boom")) == ("connection", None)


def test_network_named_exception_is_classified_as_connection():
    assert classify_fetch_error(_NetworkError("boom")) == ("connection", None)


def test_unknown_exception_falls_back_to_unknown():
    assert classify_fetch_error(RuntimeError("boom")) == ("unknown", None)


def test_non_integer_and_out_of_range_status_is_ignored():
    # bool — подкласс int: не должен пролезать как HTTP-код.
    assert classify_fetch_error(_HTTPStatusError("boom", True)) == ("unknown", None)
    assert classify_fetch_error(_HTTPStatusError("boom", "404")) == ("unknown", None)
    assert classify_fetch_error(_HTTPStatusError("boom", 999)) == ("unknown", None)


def test_result_never_carries_exception_text():
    secret = "do-not-echo-token-xyz"
    exc = _HTTPStatusError(f"GET https://api/x?token={secret} -> 403", 403)
    exc.request = type("R", (), {"url": f"https://api/x?token={secret}"})()

    result = classify_fetch_error(exc)

    assert result == ("http", 403)
    assert secret not in repr(result)
