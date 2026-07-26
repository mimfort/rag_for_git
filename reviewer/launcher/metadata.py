"""Метаданные представления команд интерактивного launcher."""

from __future__ import annotations

from reviewer.launcher.models import (
    CommandPresentation,
    Effect,
    ParameterPresentation,
    ParamSection,
)


COMMAND_PRESENTATION = {
    ("check",): CommandPresentation(
        summary="Проверить окружение",
        details="Проверяет ключи, хранилища, VCS и настроенные доски без изменения данных.",
        effects=(Effect.READ, Effect.NETWORK),
        scenarios=("После установки", "После изменения .env"),
        keywords=("health", "диагностика", "доступы"),
    ),
    ("gc",): CommandPresentation(
        summary="Очистить осиротевшие overlay",
        details="Удаляет только overlay без живой review session и просроченные сессии.",
        effects=(Effect.DESTRUCTIVE,),
        scenarios=("После прерванных ревью",),
        keywords=("cleanup", "sessions", "overlay"),
    ),
    ("index",): CommandPresentation(
        summary="Построить индекс кодовой базы",
        details="Индексирует выбранную git-ветку в векторное хранилище и граф кода.",
        effects=(Effect.WRITE, Effect.NETWORK),
        scenarios=("Первичная настройка", "Обновление base-индекса"),
        keywords=("rag", "graph", "branch"),
    ),
    ("init",): CommandPresentation(
        summary="Настроить reviewer",
        details="Создаёт или обновляет user-scope .env через существующий setup wizard.",
        effects=(Effect.WRITE,),
        scenarios=("Первичная настройка", "Смена credentials"),
        keywords=("config", "env", "wizard"),
    ),
    ("install",): CommandPresentation(
        summary="Подключить reviewer к AI-клиенту",
        details="Устанавливает MCP-конфигурацию и доступные plugin skills выбранного клиента.",
        effects=(Effect.WRITE, Effect.NETWORK),
        scenarios=("Подключение Codex/Claude/IDE",),
        keywords=("mcp", "plugin", "client"),
    ),
    ("install-skills",): CommandPresentation(
        summary="Установить только skills",
        details="Обновляет skills или plugin выбранного клиента, не меняя reviewer engine.",
        effects=(Effect.WRITE, Effect.NETWORK),
        scenarios=("Обновление workflow skills",),
        keywords=("skills", "plugin", "codex"),
    ),
    ("migrate-branches",): CommandPresentation(
        summary="Мигрировать legacy base-index",
        details="Переименовывает legacy ref base в primary branch ref после обновления.",
        effects=(Effect.WRITE,),
        scenarios=("Однократная миграция multi-branch",),
        keywords=("migration", "base", "branch"),
    ),
    ("search",): CommandPresentation(
        summary="Найти код в base-индексе",
        details="Выполняет гибридный semantic/lexical поиск по выбранной ветке.",
        effects=(Effect.READ, Effect.NETWORK),
        scenarios=("Диагностика retrieval", "Поиск символов"),
        keywords=("query", "rag", "code"),
    ),
    ("serve",): CommandPresentation(
        summary="Запустить web-админку",
        details="Запускает локальный FastAPI-сервер наблюдаемости на заданном host/port.",
        effects=(Effect.NETWORK,),
        scenarios=("Просмотр истории ревью",),
        keywords=("web", "history", "dashboard"),
    ),
    ("status",): CommandPresentation(
        summary="Проверить свежесть индекса",
        details="Показывает SHA, drift, chunks, graph nodes и overlay без Voyage-затрат.",
        effects=(Effect.READ,),
        scenarios=("Перед solve-task", "Диагностика индекса"),
        keywords=("drift", "json", "health"),
    ),
    ("update",): CommandPresentation(
        summary="Проверить обновления",
        details=(
            "Сначала только проверяет PyPI; постоянную uv tool-установку изменяет "
            "только после отдельного подтверждения."
        ),
        effects=(Effect.READ, Effect.NETWORK, Effect.WRITE),
        scenarios=("Обновление глобальной uv tool-установки",),
        keywords=("pypi", "version", "upgrade"),
        special_action="check_update",
    ),
}


PARAMETER_PRESENTATION = {
    ("check", "board_project_values"): ParameterPresentation(ParamSection.ADVANCED),
    ("index", "ref"): ParameterPresentation(ParamSection.ADVANCED),
    ("index", "branch_opt"): ParameterPresentation(ParamSection.ADVANCED),
    ("search", "branch_opt"): ParameterPresentation(ParamSection.ADVANCED),
    ("status", "branch_opt"): ParameterPresentation(ParamSection.ADVANCED),
    ("install", "path_opt"): ParameterPresentation(ParamSection.ADVANCED),
    ("install", "pin"): ParameterPresentation(ParamSection.ADVANCED),
    ("install", "no_latest"): ParameterPresentation(ParamSection.ADVANCED),
    ("install", "dry_run"): ParameterPresentation(ParamSection.ADVANCED),
    ("install-skills", "path_opt"): ParameterPresentation(ParamSection.ADVANCED),
    ("install-skills", "dry_run"): ParameterPresentation(ParamSection.ADVANCED),
}
