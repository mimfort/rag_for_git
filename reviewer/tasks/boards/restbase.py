"""Общий REST-скелет адаптера доски: httpx-клиент, retry-обёртка, secrets, URL задачи."""
from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping
from typing import Any

import httpx

from reviewer.tasks.boards.http import BoardHttpClient


class RestBoardBase:
    """База REST-адаптеров: единый транспорт вместо копий httpx-обвязки в каждом файле.

    Подклассы реализуют TaskBoardProvider поверх ``_read``/``_write`` и обязаны объявить
    ``board_type``. ``transport`` и ``sleeper`` инжектируются в тестах (без сети и без ожидания).
    """

    board_type: str = ""

    def __init__(
        self,
        *,
        base_url: str,
        secrets: Collection[str] = (),
        key_pattern: str = "",
        url_template: str = "",
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        auth: httpx.Auth | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        attempts: int = 3,
        rate_limit_hint: Callable[[int, Mapping[str, str]], float | None] | None = None,
    ) -> None:
        self._secrets = frozenset(value for value in secrets if value)
        self._key_pattern = key_pattern
        self._url_template = url_template
        self._client = httpx.Client(
            base_url=base_url,
            headers=dict(headers or {}),
            params=dict(params or {}),
            auth=auth,
            timeout=timeout,
            transport=transport,
        )
        self._http = BoardHttpClient(
            self._client,
            attempts=attempts,
            sleeper=sleeper,
            secrets=self._secrets,
            rate_limit_hint=rate_limit_hint,
        )

    @property
    def secrets(self) -> frozenset[str]:
        return self._secrets

    def _read(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._http.request_json(method, path, operation="read", **kwargs)

    def _write(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._http.request_json(method, path, operation="write", **kwargs)

    def _task_url(self, code: str) -> str:
        """Ссылка на задачу по шаблону из настроек; пустой шаблон → пустая строка."""
        if not self._url_template:
            return ""
        return self._url_template.format(code=code)

    def close(self) -> None:
        self._http.close()
