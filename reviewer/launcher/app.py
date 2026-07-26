"""Terminal UI интерактивного launcher на prompt_toolkit."""

from __future__ import annotations

import asyncio
import shlex
import threading
from collections.abc import Callable
from functools import partial

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, Layout
from prompt_toolkit.layout.containers import AnyContainer, DynamicContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import Output
from prompt_toolkit.widgets import Button, Label, TextArea

try:
    from prompt_toolkit.layout import ScrollablePane as _ScrollablePane
except ImportError:  # pragma: no cover - совместимость с ранними prompt_toolkit 3.0
    _ScrollablePane = None  # type: ignore[assignment,misc]

try:
    from prompt_toolkit.key_binding import KeyPressEvent
except ImportError:  # pragma: no cover - совместимость с prompt_toolkit 3.0.0
    from prompt_toolkit.key_binding.key_processor import KeyPressEvent

from reviewer.entrypoints.cli import cli
from reviewer.launcher.catalog import build_catalog
from reviewer.launcher.command import parameter_occurrences, prepare_command
from reviewer.launcher.controller import LauncherController, Screen
from reviewer.launcher.models import CommandSpec, LauncherResult, ParameterSpec
from reviewer.versioning import (
    InstallMode,
    InstallationInfo,
    VersionCheck,
    check_latest,
    detect_installation,
)


_EFFECT_LABELS = {
    "read": "чтение",
    "write": "запись",
    "network": "сеть",
    "destructive": "удаление",
}


class _LauncherUI:
    def __init__(
        self,
        controller: LauncherController,
        *,
        installation_detector: Callable[[], InstallationInfo],
        version_checker: Callable[..., VersionCheck],
    ) -> None:
        self.controller = controller
        self.installation_detector = installation_detector
        self.version_checker = version_checker
        self.query_field = TextArea(
            prompt="Поиск: ",
            multiline=False,
            wrap_lines=False,
        )
        self._query_handler = self._query_changed
        self.query_field.buffer.on_text_changed += self._query_handler
        self.form_widgets: list[AnyContainer] = []
        self.parameter_widgets: dict[str, AnyContainer] = {}
        self.flag_controls: dict[FormattedTextControl, str] = {}
        self.flag_buttons: dict[str, Button] = {}
        self._field_handlers: dict[TextArea, Callable[..., None]] = {}
        self.checking_update = False
        self.version_check: VersionCheck | None = None
        self.update_error: str | None = None
        self._active_update_token: object | None = None
        self._visible_update_token: object | None = None
        self._closed = False

    def container(self) -> AnyContainer:
        """Вернуть контейнер текущего экрана."""
        if self.controller.screen is Screen.PALETTE:
            return self._palette()
        if self.controller.screen is Screen.DETAILS:
            return self._details()
        if self.controller.screen is Screen.PREVIEW:
            return self._preview()
        return self._update_result()

    def open_selected(self, event: KeyPressEvent) -> None:
        """Открыть выбранную команду и подготовить поля формы."""
        self.controller.open_selected()
        if self.controller.screen is not Screen.DETAILS:
            return
        self._build_form()
        if self.form_widgets:
            event.app.layout.focus(self.form_widgets[0])
        event.app.invalidate()

    def submit_details(self, event: KeyPressEvent) -> None:
        """Открыть preview либо явно начать проверку обновлений."""
        if self.controller.selected.special_action == "check_update":
            self._start_update_check(event)
            return
        self.controller.open_preview()
        if self.controller.errors:
            first_error = next(iter(self.controller.errors))
            widget = self.parameter_widgets.get(first_error)
            if widget is not None:
                event.app.layout.focus(widget)
        event.app.invalidate()

    def confirm(self, event: KeyPressEvent) -> None:
        """Подтвердить preview или существующий Click update path."""
        if self.controller.screen is Screen.PREVIEW:
            self.controller.confirm()
        elif self.controller.screen is Screen.UPDATE_RESULT and self._can_update_uv_tool():
            self.controller.result = LauncherResult(("update",), 0)
        if self.controller.result is not None:
            self._revoke_updates()
            event.app.exit(result=self.controller.result)

    def back(self, event: KeyPressEvent) -> None:
        """Вернуться назад, не выполняя выбранную команду."""
        if self.controller.screen is Screen.PALETTE:
            self.controller.cancel()
            self._revoke_updates()
            event.app.exit(result=self.controller.result)
            return
        if self.controller.screen is Screen.UPDATE_RESULT:
            self._visible_update_token = None
            if self.update_error is not None or (
                self.version_check is not None and self.version_check.latest is None
            ):
                self.version_check = None
                self.update_error = None
            self.controller.screen = Screen.DETAILS
        else:
            self.controller.back()
        if self.controller.screen is Screen.PALETTE:
            event.app.layout.focus(self.query_field)
        event.app.invalidate()

    def cancel(self, event: KeyPressEvent, exit_code: int) -> None:
        """Завершить приложение без argv."""
        self.controller.cancel(exit_code)
        self._revoke_updates()
        event.app.exit(result=self.controller.result)

    def close(self) -> None:
        """Отозвать фоновые результаты и удалить transient UI-данные."""
        if self._closed:
            return
        self._closed = True
        self._revoke_updates()
        self.checking_update = False
        self.version_check = None
        self.update_error = None

        self.query_field.buffer.on_text_changed -= self._query_handler
        self.query_field.text = ""
        self._drop_form()
        self.controller.scrub_transient_state()

    def _revoke_updates(self) -> None:
        """Не позволить daemon-worker изменить закрывающееся приложение."""
        self._active_update_token = None
        self._visible_update_token = None

    def toggle_advanced(self, event: KeyPressEvent) -> None:
        """Переключить расширенные поля и перестроить форму."""
        if self.controller.screen is not Screen.DETAILS:
            return
        self.controller.toggle_advanced()
        self._build_form()
        if self.form_widgets:
            event.app.layout.focus(self.form_widgets[0])
        event.app.invalidate()

    def focused_flag(self, event: KeyPressEvent) -> bool:
        """Переключить флаг, если фокус установлен на его кнопке."""
        name = self.flag_controls.get(event.app.layout.current_control)
        if name is None:
            return False
        self._toggle_flag(name)
        event.app.invalidate()
        return True

    def _query_changed(self, _) -> None:
        self.controller.set_query(self.query_field.text)

    def _palette(self) -> AnyContainer:
        return HSplit(
            [
                Label("reviewer — выбор команды"),
                self.query_field,
                Window(
                    FormattedTextControl(self._command_rows),
                    height=Dimension(min=3, max=12),
                    wrap_lines=False,
                ),
                Label(self._selected_description),
                Label("↑/↓ — выбор · Enter — параметры · Esc — выход"),
            ],
            padding=1,
        )

    def _command_rows(self) -> StyleAndTextTuples:
        if not self.controller.filtered_commands:
            return [("class:empty", "  Ничего не найдено")]
        rows: StyleAndTextTuples = []
        for index, command in enumerate(self.controller.filtered_commands):
            marker = "›" if index == self.controller.selected_index else " "
            style = "class:selected" if index == self.controller.selected_index else ""
            rows.append((style, f"{marker} {' '.join(command.path):20} {command.summary}\n"))
        return rows

    def _selected_description(self) -> str:
        if not self.controller.filtered_commands:
            return "Измените поисковый запрос."
        selected = self.controller.selected
        scenarios = " · ".join(selected.scenarios)
        return f"{selected.details}\nСценарии: {scenarios}" if scenarios else selected.details

    def _details(self) -> AnyContainer:
        selected = self.controller.selected
        effects = ", ".join(_EFFECT_LABELS[effect.value] for effect in selected.effects)
        header = HSplit(
            [
                Label(f"Команда: {' '.join(selected.path)} — {selected.summary}"),
                Label(selected.details),
                Label(f"Эффекты: {effects or 'нет'}"),
            ]
        )
        if selected.special_action == "check_update":
            return HSplit(
                [
                    header,
                    Label("До подтверждения проверка только читает данные и обращается к PyPI."),
                    Label("Enter — явно проверить PyPI · Esc — назад"),
                ],
                padding=1,
            )
        if _ScrollablePane is None:
            form = self._compact_form()
        else:
            body = HSplit(
                self.form_widgets or [Label("У команды нет параметров.")],
                padding=1,
            )
            form = _ScrollablePane(body)
        advanced = "скрыты" if not self.controller.show_advanced else "показаны"
        footer = Label(
            f"Расширенные параметры: {advanced} (F2) · Tab — следующее поле · Enter — preview"
        )
        return HSplit(
            [
                header,
                form,
                footer,
            ],
            padding=1,
        )

    def _compact_form(self) -> AnyContainer:
        """Собрать компактную форму для ранних версий prompt_toolkit."""
        if not self.form_widgets:
            return Label("У команды нет параметров.")
        rows: list[AnyContainer] = []
        for parameter in self.controller.visible_parameters:
            rows.append(self.parameter_widgets[parameter.name])
            rows.append(Label(partial(self._compact_parameter_text, parameter)))
        return HSplit(rows)

    def _compact_parameter_text(self, parameter: ParameterSpec) -> str:
        """Объединить подсказку и ошибку в одну строку компактной формы."""
        return " · ".join(
            text for text in (self._parameter_hint(parameter), self._error_text(parameter)) if text
        )

    def _preview(self) -> AnyContainer:
        preview = self.controller.prepared.preview if self.controller.prepared else ""
        return HSplit(
            [
                Label("Проверьте команду перед запуском:"),
                Label(preview),
                Label("Enter — подтвердить · Esc — вернуться к параметрам"),
            ],
            padding=1,
        )

    def _update_result(self) -> AnyContainer:
        children: list[AnyContainer] = [
            Label("Проверка обновлений"),
            Label(self._update_message),
        ]
        if self._can_update_uv_tool():
            preview = prepare_command(self.controller.selected, {}, set()).preview
            children.extend(
                [
                    Label(f"Команда после подтверждения: {preview}"),
                    Label("Эффект после подтверждения: запись"),
                    Label("Внимание: будет изменена постоянная uv tool-установка rag-reviewer."),
                ]
            )
        children.append(Label(self._update_hint))
        return HSplit(children, padding=1)

    def _update_message(self) -> str:
        if self.checking_update:
            return "Проверяем способ установки и последнюю версию…"
        if self.update_error is not None:
            return f"Не удалось проверить обновления: {self.update_error}"
        if self.version_check is None:
            return "Проверка ещё не запускалась."
        check = self.version_check
        info = check.installation
        if check.latest is None:
            return (
                f"Текущая версия: {info.current}. "
                "Не удалось получить информацию с PyPI. Проверьте сеть."
            )
        if not check.current_valid:
            return (
                "Не удалось определить корректную текущую версию. "
                "Сравнение и обновление недоступны."
            )
        if check.update_available:
            return f"Доступна новая версия: {info.current} → {check.latest}"
        return f"Версия актуальна: {info.current}."

    def _update_hint(self) -> str:
        if self.update_error is not None or (
            self.version_check is not None and self.version_check.latest is None
        ):
            return "Повторить проверку: Esc — назад, затем Enter"
        if self.version_check is None:
            return "Ctrl+C — выход"
        info = self.version_check.installation
        if not self.version_check.current_valid:
            return "Исправьте установку rag-reviewer · Esc — назад"
        if self._can_update_uv_tool():
            return "Enter — подтвердить и передать команду · Esc — назад"
        if info.mode is InstallMode.EDITABLE:
            return "Для обновления: git pull && pip install -e . · Esc — назад"
        if info.mode is InstallMode.UVX:
            return (
                "MCP подхватит @latest автоматически; "
                "CLI: uvx --from rag-reviewer@latest reviewer <команда> · Esc — назад"
            )
        return "Обновление не требуется · Esc — назад"

    def _build_form(self) -> None:
        self._drop_form()
        for parameter in self.controller.visible_parameters:
            if parameter.is_flag:
                button = Button(
                    self._flag_text(parameter.name),
                    handler=partial(self._toggle_flag, parameter.name),
                    width=32,
                )
                self.flag_controls[button.control] = parameter.name
                self.flag_buttons[parameter.name] = button
                self.parameter_widgets[parameter.name] = button
                self.form_widgets.append(button)
            else:
                field = TextArea(
                    text=self._display_value(parameter, self.controller.values.get(parameter.name)),
                    prompt=f"{self._parameter_label(parameter)}: ",
                    password=parameter.sensitive,
                    multiline=False,
                    wrap_lines=False,
                )
                handler = partial(self._field_changed, parameter, field)
                field.buffer.on_text_changed += handler
                self._field_handlers[field] = handler
                self.parameter_widgets[parameter.name] = field
                self.form_widgets.append(field)
            hint = self._parameter_hint(parameter)
            if hint:
                self.form_widgets.append(Label(hint))
            self.form_widgets.append(Label(partial(self._error_text, parameter)))

    def _drop_form(self) -> None:
        """Отцепить callbacks, очистить TextArea и удалить ссылки на форму."""
        for field, handler in self._field_handlers.items():
            field.buffer.on_text_changed -= handler
            field.text = ""
        self.form_widgets.clear()
        self.parameter_widgets.clear()
        self.flag_controls.clear()
        self.flag_buttons.clear()
        self._field_handlers.clear()

    def _error_text(self, parameter: ParameterSpec) -> str:
        error = self.controller.errors.get(parameter.name)
        label = self._parameter_label(parameter)
        return f"Ошибка {label}: {error}" if error else ""

    def _field_changed(self, parameter: ParameterSpec, field: TextArea, _) -> None:
        self.controller.set_value(parameter.name, self._field_value(parameter, field.text))

    @staticmethod
    def _field_value(parameter: ParameterSpec, text: str) -> object:
        if parameter.multiple:
            return _parse_occurrences(text)
        if parameter.nargs != 1:
            try:
                return tuple(shlex.split(text))
            except ValueError:
                return (text,)
        if parameter.count:
            try:
                return int(text)
            except ValueError:
                return text
        return text or None

    @staticmethod
    def _display_value(parameter: ParameterSpec, value: object) -> str:
        if value is None:
            return ""
        if parameter.multiple:
            return " ; ".join(
                shlex.join(tuple(str(item) for item in occurrence))
                for occurrence in parameter_occurrences(value, parameter)
            )
        if isinstance(value, (tuple, list)):
            return shlex.join(tuple(str(item) for item in value))
        return str(value)

    def _toggle_flag(self, name: str) -> None:
        self.controller.set_value(name, not bool(self.controller.values.get(name)))
        self.flag_buttons[name].text = self._flag_text(name)

    def _flag_text(self, name: str) -> str:
        marker = "✓" if self.controller.values.get(name) else " "
        parameter = next(
            parameter for parameter in self.controller.selected.params if parameter.name == name
        )
        return f"[{marker}] {self._parameter_label(parameter)}"

    @staticmethod
    def _parameter_label(parameter: ParameterSpec) -> str:
        if parameter.label:
            label = parameter.label
        elif parameter.option_strings or parameter.secondary_strings:
            label = " / ".join((*parameter.option_strings, *parameter.secondary_strings))
        else:
            label = parameter.name
        if parameter.metavar and not parameter.is_flag:
            return f"{label} {parameter.metavar}"
        return label

    @staticmethod
    def _parameter_hint(parameter: ParameterSpec) -> str:
        parts = [parameter.description] if parameter.description else []
        if parameter.choices:
            parts.append(f"Варианты: {', '.join(parameter.choices)}")
        if parameter.nargs > 1:
            subject = (
                "Количество значений в каждом повторении"
                if parameter.multiple
                else "Количество значений"
            )
            parts.append(f"{subject}: {parameter.nargs}; разделяйте их пробелами.")
        if parameter.multiple:
            parts.append("Повторения разделяйте «;»; для пробелов используйте кавычки.")
        elif parameter.nargs > 1:
            parts.append("Для пробелов внутри значения используйте кавычки.")
        return " ".join(parts)

    def _start_update_check(self, event: KeyPressEvent) -> None:
        if self.version_check is not None or self.update_error is not None:
            self.controller.screen = Screen.UPDATE_RESULT
            event.app.invalidate()
            return
        if self.checking_update:
            self._visible_update_token = self._active_update_token
            self.controller.screen = Screen.UPDATE_RESULT
            event.app.invalidate()
            return
        token = object()
        self.checking_update = True
        self._active_update_token = token
        self._visible_update_token = token
        self.controller.screen = Screen.UPDATE_RESULT
        event.app.invalidate()

        loop = asyncio.get_running_loop()
        application = event.app

        def check_in_worker() -> None:
            try:
                result = self._check_update()
            except Exception as error:
                result = None
                update_error = str(error)
            else:
                update_error = None
            try:
                loop.call_soon_threadsafe(
                    self._finish_update_check,
                    token,
                    application,
                    result,
                    update_error,
                )
            except RuntimeError:
                return

        threading.Thread(
            target=check_in_worker,
            name="reviewer-update-check",
            daemon=True,
        ).start()

    def _finish_update_check(
        self,
        token: object,
        application: Application[LauncherResult],
        result: VersionCheck | None,
        update_error: str | None,
    ) -> None:
        """Принять результат проверки, пока launcher всё ещё владеет запросом."""
        if self._active_update_token is not token or application.is_done:
            return
        self.version_check = result
        self.update_error = update_error
        self.checking_update = False
        self._active_update_token = None
        owns_view = self._visible_update_token is token
        self._visible_update_token = None
        if owns_view and self.controller.result is None:
            self.controller.screen = Screen.UPDATE_RESULT
            application.invalidate()

    def _check_update(self) -> VersionCheck:
        installation = self.installation_detector()
        return self.version_checker(installation, timeout=5)

    def _can_update_uv_tool(self) -> bool:
        return (
            self.version_check is not None
            and self.version_check.current_valid
            and self.version_check.update_available
            and self.version_check.installation.mode is InstallMode.UV_TOOL
        )


def _parse_occurrences(text: str) -> tuple[tuple[str, ...], ...]:
    """Разобрать repeatable input: `;` между occurrences, shlex внутри."""
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";")
        lexer.whitespace_split = True
        lexer.commenters = ""
        occurrences: list[list[str]] = [[]]
        for token in lexer:
            if token and set(token) == {";"}:
                occurrences.extend([] for _ in token)
            else:
                occurrences[-1].append(token)
    except ValueError:
        return ((text,),)
    return tuple(tuple(occurrence) for occurrence in occurrences if occurrence)


def _bindings(ui: _LauncherUI) -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("c-c")
    def cancel_interrupt(event: KeyPressEvent) -> None:
        ui.cancel(event, 130)

    @bindings.add("escape")
    def back(event: KeyPressEvent) -> None:
        ui.back(event)

    @bindings.add("up")
    def move_up(event: KeyPressEvent) -> None:
        ui.controller.move(-1)

    @bindings.add("down")
    def move_down(event: KeyPressEvent) -> None:
        ui.controller.move(1)

    @bindings.add("tab")
    def focus_next(event: KeyPressEvent) -> None:
        event.app.layout.focus_next()

    @bindings.add("s-tab")
    def focus_previous(event: KeyPressEvent) -> None:
        event.app.layout.focus_previous()

    @bindings.add("f2")
    def toggle_advanced(event: KeyPressEvent) -> None:
        ui.toggle_advanced(event)

    @bindings.add("enter", eager=True)
    def submit(event: KeyPressEvent) -> None:
        if ui.focused_flag(event):
            return
        if ui.controller.screen is Screen.PALETTE:
            ui.open_selected(event)
        elif ui.controller.screen is Screen.DETAILS:
            ui.submit_details(event)
        else:
            ui.confirm(event)

    return bindings


def run_launcher(
    *,
    commands: tuple[CommandSpec, ...] | None = None,
    input: Input | None = None,
    output: Output | None = None,
    installation_detector: Callable[[], InstallationInfo] | None = None,
    version_checker: Callable[..., VersionCheck] | None = None,
) -> LauncherResult:
    """Запустить command palette и вернуть argv только после подтверждения."""
    controller = LauncherController(commands or build_catalog(cli))
    ui = _LauncherUI(
        controller,
        installation_detector=installation_detector or detect_installation,
        version_checker=version_checker or check_latest,
    )
    root = DynamicContainer(ui.container)
    application: Application[LauncherResult] = Application(
        layout=Layout(root, focused_element=ui.query_field),
        key_bindings=_bindings(ui),
        full_screen=True,
        input=input,
        output=output,
    )
    try:
        result = application.run()
    finally:
        ui.close()
    return result or LauncherResult(None, 0)
