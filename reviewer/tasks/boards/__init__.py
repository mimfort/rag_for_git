"""Провайдеры досок задач: фабрика по типу + реэкспорт интерфейса."""
from __future__ import annotations

from reviewer.tasks.boards.base import RawTask, TaskBoardProvider, project_prefix

__all__ = ["RawTask", "TaskBoardProvider", "project_prefix", "make_board_provider",
           "make_board_providers"]


def make_board_provider(settings, type_: str) -> TaskBoardProvider | None:
    """Сконструировать провайдер доски заданного типа из его кредов (board_creds).

    None, если у типа нет API-ключа (доска этого типа не настроена) или тип
    неизвестен — server-side синк для него недоступен.
    """
    api_key, api_base = settings.board_creds(type_)
    if not api_key:
        return None
    key_pattern = settings.task_board_key_pattern
    if type_ == "yougile":
        from reviewer.tasks.boards.yougile import YougileBoard
        return YougileBoard(
            api_key=api_key,
            api_base=api_base,
            key_pattern=key_pattern,
            url_template=settings.task_board_url_template,
            attachment_max_bytes=settings.task_attachment_max_bytes,
            attachment_timeout=settings.task_attachment_timeout,
            attachment_store_chars=settings.task_attachment_store_chars,
        )
    if type_ == "youtrack":
        from reviewer.tasks.boards.youtrack import YouTrackBoard
        return YouTrackBoard(
            token=api_key,
            base_url=api_base,
            key_pattern=key_pattern,
            attachment_max_bytes=settings.task_attachment_max_bytes,
            attachment_timeout=settings.task_attachment_timeout,
            attachment_store_chars=settings.task_attachment_store_chars,
        )
    return None


def make_board_providers(settings) -> list[TaskBoardProvider]:
    """Все настроенные доски (по configured_board_types) — для мульти-синка."""
    out: list[TaskBoardProvider] = []
    for type_ in settings.configured_board_types():
        prov = make_board_provider(settings, type_)
        if prov is not None:
            out.append(prov)
    return out
