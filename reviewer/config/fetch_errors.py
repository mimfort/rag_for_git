"""Классификация сбоя фетчера коммиченного слоя политики (PRI-234).

Модуль намеренно без зависимостей: `layers.py` не должен знать форму
HTTP-исключений VCS-клиента, а чистая функция тестируется на отсутствие
секретов в результате напрямую.
"""
from __future__ import annotations

TRANSPORT_HTTP = "http"
TRANSPORT_TIMEOUT = "timeout"
TRANSPORT_CONNECTION = "connection"
TRANSPORT_UNKNOWN = "unknown"


def _http_status(exc: BaseException) -> int | None:
    """HTTP-код из атрибута response, если он похож на настоящий код."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    # bool — подкласс int: True прошёл бы isinstance-проверку и стал бы «кодом 1».
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return status if 100 <= status <= 599 else None


def classify_fetch_error(exc: BaseException) -> tuple[str, int | None]:
    """Вернуть (transport, http_status) по форме исключения фетчера.

    Ни текст исключения, ни его args, ни request.url не читаются: там живут
    URL и токен VCS-клиента. Решение принимается только по атрибуту
    response.status_code и именам классов в MRO — они безопасны.
    """
    status = _http_status(exc)
    if status is not None:
        return TRANSPORT_HTTP, status
    names = " ".join(klass.__name__ for klass in type(exc).__mro__).lower()
    # Порядок важен: ConnectTimeout содержит и "connect", и "timeout".
    if "timeout" in names:
        return TRANSPORT_TIMEOUT, None
    if "connect" in names or "network" in names:
        return TRANSPORT_CONNECTION, None
    return TRANSPORT_UNKNOWN, None
