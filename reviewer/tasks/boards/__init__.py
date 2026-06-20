"""Провайдеры досок задач: фабрика по типу + реэкспорт интерфейса."""
from __future__ import annotations

from reviewer.tasks.boards.base import RawTask, TaskBoardProvider

__all__ = ["RawTask", "TaskBoardProvider", "make_board_provider"]


def make_board_provider(settings) -> TaskBoardProvider | None:
    """Сконструировать провайдер по task_board_default()["type"] и кредам.

    None, если доска не настроена (нет блока task_board) или нет API-ключа —
    server-side синк недоступен, sync_board вернёт понятный error-summary.
    """
    cfg = settings.task_board_default()
    if not cfg or not settings.task_board_api_key:
        return None
    type_ = cfg.get("type", "")
    if type_ == "yougile":
        from reviewer.tasks.boards.yougile import YougileBoard
        return YougileBoard(
            api_key=settings.task_board_api_key,
            api_base=settings.task_board_api_base_for(type_),
            key_pattern=cfg.get("key_pattern", ""),
            url_template=cfg.get("url_template", ""),
        )
    return None
