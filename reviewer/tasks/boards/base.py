"""Интерфейс провайдера доски задач для server-side синка (enumerate+normalize).

Транспорт изолирован за Protocol: yougile — референсная реализация (REST).
Нормализация в TaskBrief — ответственность конкретной доски (порт плейбука
task-context-<type>.md), оркестратор SyncService остаётся board-агностичным.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class RawTask:
    """Сырая задача доски до нормализации. timestamp — для watermark."""
    key: str               # канонический код (idTaskCommon, ID-N)
    project_code: str      # проектный код (idTaskProject, PRI-N)
    title: str
    description: str
    status: str | None     # резолвнутый title колонки
    subtask_ids: list[str]  # UUID подзадач (titles резолвятся в normalize)
    timestamp: int         # epoch ms последнего изменения
    links: list[dict] = field(default_factory=list)  # предрезолвленные ссылки
    # (youtrack кладёт сразу в iter_raw; yougile оставляет пустым, резолвит в normalize)


class TaskBoardProvider(Protocol):
    """Перечисление и нормализация задач доски.

    iter_raw дёшев (listing-эндпоинты); normalize дороже (best-effort резолв
    title подзадач), поэтому оркестратор зовёт normalize только для изменившихся.
    """

    board_type: str  # ключ типа доски для курсора синка (напр. "yougile", "youtrack")

    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        ...

    def normalize(self, raw: RawTask) -> dict:
        """RawTask → TaskBrief dict {key, aliases, title, description,
        criteria, status, url, links}."""
        ...
