"""Чистое состояние интерактивного launcher без зависимости от terminal UI."""

from __future__ import annotations

from enum import StrEnum

import click

from reviewer.launcher.command import (
    PreparedCommand,
    parameter_occurrences,
    prepare_command,
)
from reviewer.launcher.models import CommandSpec, LauncherResult, ParameterSpec, ParamSection


class Screen(StrEnum):
    PALETTE = "palette"
    DETAILS = "details"
    PREVIEW = "preview"
    UPDATE_RESULT = "update_result"


_TRANSITIONS = {
    (Screen.PALETTE, "open"): Screen.DETAILS,
    (Screen.DETAILS, "preview"): Screen.PREVIEW,
    (Screen.PREVIEW, "back"): Screen.DETAILS,
    (Screen.DETAILS, "back"): Screen.PALETTE,
}

_SAFE_CLICK_TYPES = (
    click.types.StringParamType,
    click.types.IntParamType,
    click.types.FloatParamType,
    click.types.BoolParamType,
    click.types.Choice,
    click.types.DateTime,
    click.types.UUIDParameterType,
)


class LauncherController:
    """Управлять выбором, формой и явным подтверждением команды."""

    def __init__(self, commands: tuple[CommandSpec, ...]) -> None:
        self.commands = commands
        self.query = ""
        self.filtered_commands = commands
        self.selected_index = 0
        self.screen = Screen.PALETTE
        self.values: dict[str, object] = {}
        self.changed: set[str] = set()
        self.errors: dict[str, str] = {}
        self.show_advanced = False
        self.prepared: PreparedCommand | None = None
        self.result: LauncherResult | None = None

    @property
    def selected(self) -> CommandSpec:
        """Вернуть выбранную команду."""
        return self.filtered_commands[self.selected_index]

    @property
    def visible_parameters(self) -> tuple[ParameterSpec, ...]:
        """Вернуть основные и, по запросу, расширенные поля формы."""
        if self.screen not in (Screen.DETAILS, Screen.PREVIEW):
            return ()
        return tuple(
            parameter
            for parameter in self.selected.params
            if self.show_advanced or parameter.section is ParamSection.BASIC
        )

    def set_query(self, query: str) -> None:
        """Отфильтровать каталог и выбрать наиболее точное совпадение."""
        if self.screen is not Screen.PALETTE:
            return
        self.query = query
        normalized = query.strip().casefold()
        ranked = [
            (self._match_rank(command, normalized), position, command)
            for position, command in enumerate(self.commands)
        ]
        self.filtered_commands = tuple(
            command for rank, _, command in sorted(item for item in ranked if item[0] is not None)
        )
        self.selected_index = 0

    def move(self, delta: int) -> None:
        """Сдвинуть выбор по циклу внутри отфильтрованного списка."""
        if self.screen is not Screen.PALETTE or not self.filtered_commands:
            return
        self.selected_index = (self.selected_index + delta) % len(self.filtered_commands)

    def open_selected(self) -> None:
        """Открыть форму выбранной команды."""
        next_screen = _TRANSITIONS.get((self.screen, "open"))
        if next_screen is None or not self.filtered_commands:
            return
        self.screen = next_screen
        self.values = {
            parameter.name: self._initial_value(parameter) for parameter in self.selected.params
        }
        self.changed.clear()
        self.errors.clear()
        self.show_advanced = False
        self.prepared = None

    def set_value(self, name: str, value: object) -> None:
        """Изменить поле открытой формы."""
        if self.screen is not Screen.DETAILS:
            return
        if name not in {parameter.name for parameter in self.selected.params}:
            return
        self.values[name] = value
        self.changed.add(name)
        self.errors.pop(name, None)

    def toggle_advanced(self) -> None:
        """Показать или скрыть расширенные параметры."""
        if self.screen is Screen.DETAILS:
            self.show_advanced = not self.show_advanced

    def open_preview(self) -> None:
        """Проверить безопасные типы и открыть preview без выполнения команды."""
        next_screen = _TRANSITIONS.get((self.screen, "preview"))
        if next_screen is None:
            return
        self.errors = self._validate()
        if self.errors:
            return
        self.prepared = prepare_command(self.selected, self.values, self.changed)
        self.screen = next_screen

    def confirm(self) -> None:
        """Вернуть argv только после отдельного подтверждения preview."""
        if self.screen is not Screen.PREVIEW or self.prepared is None:
            return
        self.result = LauncherResult(self.prepared.argv, 0)

    def back(self) -> None:
        """Вернуться на предыдущий экран без запуска команды."""
        next_screen = _TRANSITIONS.get((self.screen, "back"))
        if next_screen is None:
            return
        self.screen = next_screen
        if next_screen is Screen.PALETTE:
            self.prepared = None
            self.errors.clear()

    def cancel(self, exit_code: int = 0) -> None:
        """Завершить launcher без выбранной команды."""
        self.result = LauncherResult(None, exit_code)

    def scrub_transient_state(self) -> None:
        """Удалить введённые значения, оставив только immutable результат запуска."""
        self.query = ""
        self.values.clear()
        self.changed.clear()
        self.errors.clear()
        self.prepared = None
        self.show_advanced = False

    @staticmethod
    def _initial_value(parameter: ParameterSpec) -> object:
        default = parameter.default
        if callable(default) or type(default).__name__ == "Sentinel":
            return None
        return default

    @staticmethod
    def _match_rank(command: CommandSpec, query: str) -> tuple[int, ...] | None:
        if not query:
            return (0,)
        fields = (
            " ".join(command.path),
            command.summary,
            *command.keywords,
            command.details,
            *command.scenarios,
        )
        matches = [
            (*score, position)
            for position, text in enumerate(fields)
            if (score := _fuzzy_score(text.casefold(), query)) is not None
        ]
        return min(matches) if matches else None

    def _validate(self) -> dict[str, str]:
        errors: dict[str, str] = {}
        for parameter in self.selected.params:
            value = self.values.get(parameter.name)
            if parameter.required and self._is_empty(value):
                errors[parameter.name] = "Обязательное поле"
                continue
            if self._is_empty(value):
                continue
            occurrences = parameter_occurrences(value, parameter)
            if parameter.nargs > 0 and any(
                len(occurrence) != parameter.nargs for occurrence in occurrences
            ):
                errors[parameter.name] = (
                    f"Количество значений в каждом повторении должно быть равно {parameter.nargs}"
                )
                continue
            if type(parameter.source.type) not in _SAFE_CLICK_TYPES:
                continue
            try:
                self._convert(parameter, occurrences)
            except click.BadParameter as error:
                errors[parameter.name] = (
                    "Некорректное значение" if parameter.sensitive else error.format_message()
                )
        return errors

    @staticmethod
    def _is_empty(value: object) -> bool:
        return value is None or value == "" or value == () or value == []

    @staticmethod
    def _convert(parameter: ParameterSpec, occurrences: tuple[tuple[object, ...], ...]) -> None:
        for occurrence in occurrences:
            for item in occurrence:
                parameter.source.type.convert(item, parameter.source, None)


def _fuzzy_score(text: str, query: str) -> tuple[int, int, int, int] | None:
    if text == query:
        return (0, 0, 0, len(text))
    if text.startswith(query):
        return (1, len(text) - len(query), 0, len(text))
    substring_start = text.find(query)
    if substring_start >= 0:
        return (2, substring_start, len(text) - len(query), len(text))

    positions: list[int] = []
    search_from = 0
    for character in query:
        position = text.find(character, search_from)
        if position < 0:
            return None
        positions.append(position)
        search_from = position + 1
    span = positions[-1] - positions[0] + 1
    return (3, span - len(query), positions[0], len(text))
