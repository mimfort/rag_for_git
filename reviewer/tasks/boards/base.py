"""Интерфейс провайдера доски задач для server-side синка (enumerate+normalize).

Транспорт изолирован за Protocol: yougile — референсная реализация (REST).
Нормализация в TaskBrief — ответственность конкретной доски (порт плейбука
task-context-<type>.md), оркестратор SyncService остаётся board-агностичным.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from reviewer.config.task_board import TaskSyncFilter

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

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
    timestamp: int | None  # epoch ms последнего изменения
    links: list[dict] = field(default_factory=list)  # предрезолвленные ссылки
    # (youtrack кладёт сразу в iter_raw; yougile оставляет пустым, резолвит в normalize)
    attachments: list[dict] = field(default_factory=list)  # метаданные вложений из iter_raw
    # (youtrack: name/mime/size/url inline из _FIELDS; yougile: пусто, фетчится в normalize)
    board_id: str = ""  # внутренний id задачи у провайдера (yougile UUID для чат-эндпоинта;
    # youtrack не использует — там везде idReadable)
    archived: bool | None = None
    terminal: bool | None = None
    provider_data: dict = field(default_factory=dict)  # нейтральные расширенные метаданные


@dataclass
class TaskListingStats:
    filtered_by_age: int = 0
    filtered_archived: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class TaskListing:
    rows: Iterable[RawTask]
    stats: TaskListingStats = field(default_factory=TaskListingStats)

    def __iter__(self) -> Iterator[RawTask]:
        return iter(self.rows)


@dataclass(frozen=True)
class NativeSubtaskIdentity:
    board_id: str
    key: str
    title: str
    aliases: tuple[str, ...] = ()
    url: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconciledNativeSubtask:
    marker: str
    identity: NativeSubtaskIdentity


class NativeSubtaskProvider(Protocol):
    """Опциональные операции провайдера с нативными подзадачами."""

    def reconcile_native_subtasks(
        self,
        source_board_id: str,
        markers: frozenset[str],
    ) -> list[ReconciledNativeSubtask]:
        ...

    def create_native_subtask(
        self,
        doc_md: str,
        *,
        title: str,
        source_column_id: str,
        marker: str,
    ) -> NativeSubtaskIdentity:
        ...

    def replace_native_subtasks(
        self,
        parent_task_id: str,
        subtask_ids: list[str],
    ) -> None:
        ...


class TaskBoardProvider(Protocol):
    """Перечисление и нормализация задач доски.

    iter_raw дёшев (listing-эндпоинты); normalize дороже (best-effort резолв
    title подзадач), поэтому оркестратор зовёт normalize только для изменившихся.
    """

    board_type: str  # ключ типа доски для курсора синка (напр. "yougile", "youtrack")

    def validate_connection(self, project: str | None = None) -> dict:
        ...

    def iter_raw(
        self,
        board: str | None,
        limit: int | None,
        *,
        sync_filter: TaskSyncFilter | None = None,
        now_ms: int | None = None,
    ) -> TaskListing:
        ...

    def normalize(self, raw: RawTask) -> dict:
        """RawTask → TaskBrief dict {key, aliases, title, description,
        criteria, status, url, links, attachments}.

        ИНВАРИАНТ: description возвращается в markdown. Если транспорт доски
        хранит другой формат (YouGile — HTML), конвертация делается внутри
        провайдера (reviewer/tasks/boards/markup.py).
        """
        ...

    def normalize_meta(self, raw: RawTask) -> dict:
        """Дешёвый TaskBrief из RawTask БЕЗ I/O (PRI-207): только плоские
        метаданные (key, aliases, title, status, url, project). Подзадачи и
        вложения НЕ резолвятся (criteria=[], attachments=[]). Для self-healing
        meta-refresh задач ниже watermark — не дёргает сеть на задачу."""
        ...

    def list_targets(self, project: str | None) -> dict:
        ...

    def finish(self, key: str, pr_url: str, *, note: str | None = None,
               mark_done: bool = True, target: str | None = None) -> dict:
        """Закрыть задачу: пометить done + идемпотентно дописать PR-ссылку в описание.
        Любая правка двигает last-modified (timestamp/updated) → инкрементальный синк
        переиндексирует обновлённую задачу. target — board-специфичная done-цель
        (например, id/label колонки или значение status). Возвращает
        {key, board_id, done_set, pr_link_added, already_closed, warnings};
        YouGile дополнительно кладёт `column_moved: bool` (доска-специфичное поле)."""
        ...

    def fetch_one(self, key: str) -> RawTask | None:
        """Один RawTask по ключу задачи — для write-through после finish.

        После finish закрытую задачу надо сразу переиндексировать в стор reviewer,
        не дожидаясь инкрементального sync_board (тот отсекает задачи с
        timestamp <= watermark-курсор). ``None`` означает только достоверное
        отсутствие задачи; ошибки чтения провайдер обязан пробрасывать."""
        ...

    def create(self, doc_md: str, *, title: str, target: str | None,
               project: str | None) -> dict:
        """Создать задачу из канонического markdown (см. reviewer/tasks/taskdoc.py).

        target — доска-специфичная цель размещения (YouGile: title колонки;
        YouTrack: значение поля статуса); не найдена → создать в дефолтном месте
        и вернуть причину в warnings, не падать. Возвращает
        {key, url, board_id, target_resolved, warnings}. Бросает только если
        задачу не удалось создать вовсе.

        target_resolved — фактически применённая колонка/статус, а не эхо
        запрошенного target: семантика различается по доскам. YouGile
        возвращает title колонки ВСЕГДА (запрошенная — если target совпал,
        иначе дефолтная первая колонка как fallback), в том числе когда target
        вообще не запрашивался. YouTrack возвращает None и когда target не
        запрашивался, и когда резолв/применение не удалось. Поэтому
        `bool(target_resolved)` НЕ означает «запрошенный target применился» —
        потребитель должен сравнивать `target_resolved` с запрошенным
        `target`, а не проверять его на truthiness.
        """
        ...

    def close(self) -> None:
        ...
