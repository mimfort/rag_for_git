"""Общая инфраструктура фейков: состояние, записывающий транспорт, хелперы, ProviderAdapter.

``ProviderAdapter`` объявлен здесь, а не в ``contract.py``, чтобы пофайловые фейки
импортировали его без циклического импорта: этот модуль ничего из ``fakes`` не тянет.
``contract.py`` реэкспортирует тип, поэтому оба пути импорта рабочие.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from reviewer.tasks.boards.base import TaskBoardProvider


@dataclass
class FakeState:
    """Наблюдаемое состояние фейка: вызовы и факт закрытия транспорта."""

    calls: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    closed: bool = False


class RecordingTransport(httpx.MockTransport):
    """MockTransport, отмечающий в состоянии факт закрытия транспорта."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response], state: FakeState):
        super().__init__(handler)
        self._state = state

    def close(self) -> None:
        self._state.closed = True
        super().close()


def record(state: FakeState, request: httpx.Request) -> None:
    """Записать вызов (метод, путь, query-параметры) в состояние фейка."""
    state.calls.append(
        (request.method, request.url.path, dict(request.url.params.multi_items()))
    )


def request_json(request: httpx.Request) -> dict[str, Any]:
    """Тело запроса как dict; пустое тело → пустой dict."""
    return json.loads(request.content.decode()) if request.content else {}


@dataclass(frozen=True)
class ProviderAdapter:
    """Фейк одного провайдера для общего contract-набора: реквизиты, фабрика, пороги."""

    board_type: str
    secret: str
    project: str
    key: str
    finish_key: str
    target_id: str
    target_label: str
    missing_target: str
    factory: Callable[..., tuple[TaskBoardProvider, FakeState]]
    min_rows: int
    page_paths: tuple[str, ...]

    def provider_factory(
        self,
        *,
        state: FakeState | None = None,
        forbidden: bool = False,
        error_status: int | None = None,
    ) -> tuple[TaskBoardProvider, FakeState]:
        return self.factory(state=state, forbidden=forbidden, error_status=error_status)
