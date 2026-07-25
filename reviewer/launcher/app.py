"""Terminal UI интерактивного launcher на prompt_toolkit."""
from __future__ import annotations

import asyncio
import shlex
from collections.abc import Callable
from functools import partial

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import Dimension, Layout
from prompt_toolkit.layout.containers import AnyContainer, DynamicContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import Output
from prompt_toolkit.widgets import Button, Label, TextArea

from reviewer.entrypoints.cli import cli
from reviewer.launcher.catalog import build_catalog
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
        self.query_field.buffer.on_text_changed += self._query_changed
        self.form_widgets: list[AnyContainer] = []
        self.parameter_widgets: dict[str, AnyContainer] = {}
        self.flag_controls: dict[FormattedTextControl, str] = {}
        self.flag_buttons: dict[str, Button] = {}
        self.checking_update = False
        self.version_check: VersionCheck | None = None
        self.update_error: str | None = None
        self._active_update_token: object | None = None
        self._visible_update_token: object | None = None

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
            event.app.exit(result=self.controller.result)

    def back(self, event: KeyPressEvent) -> None:
        """Вернуться назад, не выполняя выбранную команду."""
        if self.controller.screen is Screen.PALETTE:
            self.controller.cancel()
            event.app.exit(result=self.controller.result)
            return
        if self.controller.screen is Screen.UPDATE_RESULT:
            self._visible_update_token = None
            self.controller.screen = Screen.DETAILS
        else:
            self.controller.back()
        if self.controller.screen is Screen.PALETTE:
            event.app.layout.focus(self.query_field)
        event.app.invalidate()

    def cancel(self, event: KeyPressEvent, exit_code: int) -> None:
        """Завершить приложение без argv."""
        self.controller.cancel(exit_code)
        event.app.exit(result=self.controller.result)

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
        header = [
            Label(f"Команда: {' '.join(selected.path)} — {selected.summary}"),
            Label(selected.details),
            Label(f"Эффекты: {effects or 'нет'}"),
        ]
        if selected.special_action == "check_update":
            body: list[AnyContainer] = [
                Label("Enter — явно проверить PyPI · Esc — назад"),
            ]
        else:
            body = self.form_widgets or [Label("У команды нет параметров.")]
            advanced = "скрыты" if not self.controller.show_advanced else "показаны"
            body = [
                *body,
                Label(
                    f"Расширенные параметры: {advanced} (F2) · "
                    "Tab — следующее поле · Enter — preview"
                ),
            ]
        return HSplit([*header, *body], padding=1)

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
        return HSplit(
            [
                Label("Проверка обновлений"),
                Label(self._update_message),
                Label(self._update_hint),
            ],
            padding=1,
        )

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
        if check.update_available:
            return f"Доступна новая версия: {info.current} → {check.latest}"
        return f"Версия актуальна: {info.current}."

    def _update_hint(self) -> str:
        if self.version_check is None:
            return "Ctrl+C — выход"
        info = self.version_check.installation
        if self._can_update_uv_tool():
            return "Enter — передать команду существующему Click update · Esc — назад"
        if info.mode is InstallMode.EDITABLE:
            return "Для обновления: git pull && pip install -e . · Esc — назад"
        if info.mode is InstallMode.UVX:
            return (
                "MCP подхватит @latest автоматически; "
                "CLI: uvx --from rag-reviewer@latest reviewer <команда> · Esc — назад"
            )
        return "Обновление не требуется · Esc — назад"

    def _build_form(self) -> None:
        self.form_widgets = []
        self.parameter_widgets = {}
        self.flag_controls = {}
        self.flag_buttons = {}
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
                    text=self._display_value(self.controller.values.get(parameter.name)),
                    prompt=f"{parameter.name}: ",
                    password=parameter.sensitive,
                    multiline=False,
                    wrap_lines=False,
                )
                field.buffer.on_text_changed += partial(self._field_changed, parameter, field)
                self.parameter_widgets[parameter.name] = field
                self.form_widgets.append(field)
            self.form_widgets.append(Label(partial(self._error_text, parameter.name)))

    def _error_text(self, name: str) -> str:
        error = self.controller.errors.get(name)
        return f"Ошибка {name}: {error}" if error else ""

    def _field_changed(self, parameter: ParameterSpec, field: TextArea, _) -> None:
        self.controller.set_value(parameter.name, self._field_value(parameter, field.text))

    @staticmethod
    def _field_value(parameter: ParameterSpec, text: str) -> object:
        if parameter.multiple:
            return tuple(item.strip() for item in text.split(",") if item.strip())
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
    def _display_value(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, (tuple, list)):
            return ", ".join(str(item) for item in value)
        return str(value)

    def _toggle_flag(self, name: str) -> None:
        self.controller.set_value(name, not bool(self.controller.values.get(name)))
        self.flag_buttons[name].text = self._flag_text(name)

    def _flag_text(self, name: str) -> str:
        marker = "✓" if self.controller.values.get(name) else " "
        return f"[{marker}] {name}"

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

        async def check_in_executor() -> None:
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, self._check_update)
            except Exception as error:
                result = None
                update_error = str(error)
            else:
                update_error = None
            if self._active_update_token is token:
                self.version_check = result
                self.update_error = update_error
                self.checking_update = False
                self._active_update_token = None
                owns_view = self._visible_update_token is token
                self._visible_update_token = None
                if owns_view and self.controller.result is None:
                    self.controller.screen = Screen.UPDATE_RESULT
                    event.app.invalidate()

        event.app.create_background_task(check_in_executor())

    def _check_update(self) -> VersionCheck:
        installation = self.installation_detector()
        return self.version_checker(installation, timeout=5)

    def _can_update_uv_tool(self) -> bool:
        return (
            self.version_check is not None
            and self.version_check.update_available
            and self.version_check.installation.mode is InstallMode.UV_TOOL
        )


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
    result = application.run()
    return result or LauncherResult(None, 0)
