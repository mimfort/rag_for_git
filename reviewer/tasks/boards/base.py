"""Интерфейс провайдера доски задач для server-side синка (enumerate+normalize).

Транспорт изолирован за Protocol: yougile — референсная реализация (REST).
Нормализация в TaskBrief — ответственность конкретной доски (порт плейбука
task-context-<type>.md), оркестратор SyncService остаётся board-агностичным.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)-\d+$")


def project_prefix(code: str) -> str:
    """Префикс проекта из кода задачи: ``PRI-5`` → ``PRI``. ``""`` если не код вида
    ``<PREFIX>-<число>`` (метка скоупа по проекту, PRI-170)."""
    m = _PREFIX_RE.match(code or "")
    return m.group(1) if m else ""


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
    attachments: list[dict] = field(default_factory=list)  # метаданные вложений из iter_raw
    # (youtrack: name/mime/size/url inline из _FIELDS; yougile: пусто, фетчится в normalize)
    board_id: str = ""  # внутренний id задачи у провайдера (yougile UUID для чат-эндпоинта;
    # youtrack не использует — там везде idReadable)
    completed: bool = False  # YouGile: булев чекбокс «выполнено» (мапится в status="done")


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
        criteria, status, url, links, attachments}."""
        ...

    def finish(self, key: str, pr_url: str, *, note: str | None = None,
               mark_done: bool = True, done_state: str | None = None,
               done_column: str | None = None) -> dict:
        """Закрыть задачу: пометить done + идемпотентно дописать PR-ссылку в описание.
        Любая правка двигает last-modified (timestamp/updated) → инкрементальный синк
        переиндексирует обновлённую задачу. done_state — целевое состояние (YouTrack;
        YouGile игнорирует, у него булев completed). done_column — целевая колонка
        (YouGile: перенос задачи; YouTrack игнорирует). Возвращает
        {key, board_id, done_set, pr_link_added, already_closed, warnings};
        YouGile дополнительно кладёт `column_moved: bool` (доска-специфичное поле)."""
        ...
