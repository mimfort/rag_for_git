from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from io import StringIO

import click

import reviewer.launcher.app as launcher_app
from reviewer.entrypoints.cli import cli
from reviewer.launcher.app import _LauncherUI, _bindings, run_launcher
from reviewer.launcher.catalog import build_catalog
from reviewer.launcher.controller import LauncherController, Screen
from reviewer.launcher.models import CommandSpec, LauncherResult, ParameterSpec, ParamSection
from reviewer.versioning import InstallMode, InstallationInfo, VersionCheck

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import DummyInput, create_pipe_input
from prompt_toolkit.layout import Layout
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.plain_text import PlainTextOutput


def _spec(name: str) -> CommandSpec:
    return next(item for item in build_catalog(cli) if item.path == (name,))


def _integer_spec() -> CommandSpec:
    source = click.Option(["--port"], type=int)
    parameter = ParameterSpec(
        source=source,
        name="port",
        kind="option",
        option_strings=("--port",),
        secondary_strings=(),
        required=False,
        nargs=1,
        multiple=False,
        count=False,
        is_flag=False,
        default=None,
        choices=(),
        section=ParamSection.BASIC,
        sensitive=False,
    )
    return CommandSpec(
        path=("integer",),
        command=click.Command("integer", params=[source]),
        summary="Проверить целое число",
        details="Тестовая команда с целочисленным параметром.",
        effects=(),
        scenarios=(),
        keywords=(),
        params=(parameter,),
    )


def _sensitive_integer_spec() -> CommandSpec:
    source = click.Option(["--token"], type=int)
    parameter = ParameterSpec(
        source=source,
        name="token",
        kind="option",
        option_strings=("--token",),
        secondary_strings=(),
        required=False,
        nargs=1,
        multiple=False,
        count=False,
        is_flag=False,
        default=None,
        choices=(),
        section=ParamSection.BASIC,
        sensitive=True,
    )
    return CommandSpec(
        path=("sensitive",),
        command=click.Command("sensitive", params=[source]),
        summary="Проверить секрет",
        details="Тестовая команда с чувствительным целочисленным параметром.",
        effects=(),
        scenarios=(),
        keywords=(),
        params=(parameter,),
    )


def _choice_spec() -> CommandSpec:
    @click.group()
    def root() -> None:
        pass

    @root.command()
    @click.option(
        "--mode",
        "internal_mode",
        type=click.Choice(("fast", "safe")),
        help="Режим выполнения из Click.",
    )
    def choose(internal_mode: str | None) -> None:
        pass

    return build_catalog(root)[0]


def _send_after(
    pipe,
    event: threading.Event,
    keys: bytes,
    *,
    timeout: float = 2,
) -> threading.Thread:
    def sender() -> None:
        if not event.wait(timeout):
            pipe.send_bytes(b"\x03")
            return
        time.sleep(0.05)
        pipe.send_bytes(keys)

    thread = threading.Thread(target=sender)
    thread.start()
    return thread


class _TrackingOutput(PlainTextOutput):
    def __init__(self) -> None:
        self.stream = StringIO()
        self.progress_rendered = threading.Event()
        self.required_error_rendered = threading.Event()
        self.required_error_removed = threading.Event()
        self.type_error_rendered = threading.Event()
        self.sensitive_error_rendered = threading.Event()
        self.public_fields_rendered = threading.Event()
        self.choice_help_rendered = threading.Event()
        self.update_result_rendered = threading.Event()
        self._frame = ""
        super().__init__(self.stream)

    def write(self, data: str) -> None:
        super().write(data)
        self._frame += data
        rendered = self.stream.getvalue()
        if "Проверяем способ установки" in rendered:
            self.progress_rendered.set()
        if (
            "Ошибка REPO: Обязательное поле" in rendered
            and not self.required_error_rendered.is_set()
        ):
            self.required_error_rendered.set()
        if "Ошибка --port:" in rendered:
            self.type_error_rendered.set()
        if "Ошибка --token:" in rendered:
            self.sensitive_error_rendered.set()
        if "--repo:" in rendered and "--branch:" in rendered:
            self.public_fields_rendered.set()
        if "Режим выполнения из Click." in rendered and "Варианты: fast, safe" in rendered:
            self.choice_help_rendered.set()
        if "Доступна новая версия: 0.4.0 → 0.5.0" in rendered:
            self.update_result_rendered.set()
        if (
            self.required_error_rendered.is_set()
            and "owner/repo" in self._frame
            and "Расширенные параметры:" in self._frame
            and "Ошибка REPO:" not in self._frame
        ):
            self.required_error_removed.set()

    def erase_down(self) -> None:
        self._frame = ""
        super().erase_down()


class _SmallTerminalOutput(_TrackingOutput):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[str] = []
        self.focused_frames: dict[str, str] = {}
        self.client_focused = threading.Event()
        self.path_focused = threading.Event()
        self.pin_focused = threading.Event()
        self.dry_run_focused = threading.Event()
        self.dry_run_toggled = threading.Event()

    def get_size(self) -> Size:
        return Size(rows=24, columns=80)

    def flush(self) -> None:
        super().flush()
        screen = get_app().renderer.last_rendered_screen
        if screen is None:
            return
        size = self.get_size()
        frame = "\n".join(
            "".join(screen.data_buffer[row][column].char for column in range(size.columns))
            for row in range(size.rows)
        )
        self.frames.append(frame)
        write_position = screen.visible_windows_to_write_positions.get(
            get_app().layout.current_window
        )
        if write_position is None:
            return
        focused_text = "\n".join(
            frame.splitlines()[
                max(0, write_position.ypos) : max(0, write_position.ypos + write_position.height)
            ]
        )
        focused = (
            ("client", "CLIENT:", self.client_focused),
            ("path", "--path:", self.path_focused),
            ("pin", "--pin:", self.pin_focused),
            ("dry_run", "--dry-run", self.dry_run_focused),
        )
        for name, marker, event in focused:
            if marker in focused_text:
                self.focused_frames[name] = frame
                event.set()
        if "[✓] --dry-run" in focused_text:
            self.focused_frames["dry_run_toggled"] = frame
            self.dry_run_toggled.set()


def test_escape_from_palette_returns_clean_cancel():
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\x1b")
        result = run_launcher(
            commands=(_spec("status"),),
            input=pipe,
            output=DummyOutput(),
        )

    assert result == LauncherResult(None, 0)


def test_ctrl_c_returns_shell_interrupt_code():
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\x03")
        result = run_launcher(
            commands=(_spec("status"),),
            input=pipe,
            output=DummyOutput(),
        )

    assert result == LauncherResult(None, 130)


def test_regular_command_returns_argv_only_after_preview_confirm():
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\r\r\r")
        result = run_launcher(
            commands=(_spec("status"),),
            input=pipe,
            output=DummyOutput(),
        )

    assert result == LauncherResult(("status",), 0)


def test_required_error_is_rendered_and_removed_after_field_edit():
    output = _TrackingOutput()

    with create_pipe_input() as pipe:

        def stop_launcher() -> None:
            output.required_error_removed.wait(2)
            pipe.send_bytes(b"\x03")

        edit_field = threading.Timer(0.5, lambda: pipe.send_text("owner/repo"))
        wake_input = threading.Timer(1, lambda: pipe.send_bytes(b"\x1b[C"))
        stop = threading.Thread(target=stop_launcher)
        edit_field.start()
        wake_input.start()
        stop.start()
        pipe.send_bytes(b"\r\r")
        result = run_launcher(
            commands=(_spec("index"),),
            input=pipe,
            output=output,
        )
        edit_field.join()
        wake_input.join()
        stop.join()

    assert result == LauncherResult(None, 130)
    assert output.required_error_rendered.is_set(), output.stream.getvalue()
    assert output.required_error_removed.is_set(), output.stream.getvalue()


def test_builtin_type_error_is_rendered_in_details():
    output = _TrackingOutput()

    def sender(pipe) -> None:
        output.type_error_rendered.wait(2)
        pipe.send_bytes(b"\x03")

    with create_pipe_input() as pipe:
        thread = threading.Thread(target=sender, args=(pipe,))
        thread.start()
        pipe.send_bytes(b"\r")
        pipe.send_text("invalid")
        pipe.send_bytes(b"\r")
        result = run_launcher(
            commands=(_integer_spec(),),
            input=pipe,
            output=output,
        )
        thread.join()

    assert result == LauncherResult(None, 130)
    assert output.type_error_rendered.is_set(), output.stream.getvalue()


def test_sensitive_type_error_is_rendered_without_raw_value():
    output = _TrackingOutput()
    secret = "secret-integer"

    def sender(pipe) -> None:
        output.sensitive_error_rendered.wait(2)
        pipe.send_bytes(b"\x03")

    with create_pipe_input() as pipe:
        thread = threading.Thread(target=sender, args=(pipe,))
        thread.start()
        pipe.send_bytes(b"\r")
        pipe.send_text(secret)
        pipe.send_bytes(b"\r")
        result = run_launcher(
            commands=(_sensitive_integer_spec(),),
            input=pipe,
            output=output,
        )
        thread.join()

    rendered = output.stream.getvalue()
    assert result == LauncherResult(None, 130)
    assert output.sensitive_error_rendered.is_set(), rendered
    assert "Ошибка --token: Некорректное значение" in rendered
    assert secret not in rendered


def test_details_render_public_option_labels_including_advanced_fields():
    output = _TrackingOutput()

    def sender(pipe) -> None:
        output.public_fields_rendered.wait(2)
        pipe.send_bytes(b"\x03")

    with create_pipe_input() as pipe:
        thread = threading.Thread(target=sender, args=(pipe,))
        thread.start()
        pipe.send_bytes(b"\r\x1bOQ")
        result = run_launcher(
            commands=(_spec("status"),),
            input=pipe,
            output=output,
        )
        thread.join()

    rendered = output.stream.getvalue()
    assert result == LauncherResult(None, 130)
    assert output.public_fields_rendered.is_set(), rendered
    assert "repo_tag" not in rendered
    assert "branch_opt" not in rendered


def test_details_render_click_help_and_choices():
    output = _TrackingOutput()

    def sender(pipe) -> None:
        output.choice_help_rendered.wait(2)
        pipe.send_bytes(b"\x03")

    with create_pipe_input() as pipe:
        thread = threading.Thread(target=sender, args=(pipe,))
        thread.start()
        pipe.send_bytes(b"\r")
        result = run_launcher(
            commands=(_choice_spec(),),
            input=pipe,
            output=output,
        )
        thread.join()

    rendered = output.stream.getvalue()
    assert result == LauncherResult(None, 130)
    assert output.choice_help_rendered.is_set(), rendered
    assert "internal_mode" not in rendered


def test_advanced_install_fields_scroll_in_24_by_80_terminal():
    output = _SmallTerminalOutput()

    def sender(pipe) -> None:
        try:
            if not output.client_focused.wait(2):
                return
            pipe.send_bytes(b"\t\t\t")
            if not output.path_focused.wait(2):
                return
            pipe.send_bytes(b"\t")
            if not output.pin_focused.wait(2):
                return
            pipe.send_bytes(b"\t\t\t")
            if not output.dry_run_focused.wait(2):
                return
            pipe.send_bytes(b"\r")
            output.dry_run_toggled.wait(2)
        finally:
            pipe.send_bytes(b"\x03")

    with create_pipe_input() as pipe:
        thread = threading.Thread(target=sender, args=(pipe,))
        thread.start()
        pipe.send_bytes(b"\r\x1bOQ")
        result = run_launcher(
            commands=(_spec("install"),),
            input=pipe,
            output=output,
        )
        thread.join()

    rendered = "\n".join(output.frames)
    assert result == LauncherResult(None, 130)
    assert "Window too small" not in rendered
    assert output.path_focused.is_set(), rendered
    assert output.pin_focused.is_set(), rendered
    assert output.dry_run_focused.is_set(), rendered
    assert output.dry_run_toggled.is_set(), rendered
    for name in ("path", "pin", "dry_run", "dry_run_toggled"):
        assert "Расширенные параметры: показаны" in output.focused_frames[name]


def test_advanced_install_has_compact_fallback_without_scrollable_pane(monkeypatch):
    monkeypatch.setattr(launcher_app, "_ScrollablePane", None)
    controller = LauncherController((_spec("install"),))
    controller.open_selected()
    controller.toggle_advanced()
    ui = _LauncherUI(
        controller,
        installation_detector=lambda: None,
        version_checker=lambda installation, timeout: None,
    )
    ui._build_form()
    output = _SmallTerminalOutput()

    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\t\t\t\t\t\t\t\x03")
        application: Application[LauncherResult] = Application(
            layout=Layout(ui._details(), focused_element=ui.form_widgets[0]),
            key_bindings=_bindings(ui),
            full_screen=True,
            input=pipe,
            output=output,
        )
        result = application.run()

    rendered = "\n".join(output.frames)
    assert result == LauncherResult(None, 130)
    assert "Window too small" not in rendered
    assert "--path:" in rendered
    assert "--pin:" in rendered
    assert "--dry-run" in rendered
    assert "Расширенные параметры: показаны" in rendered


def test_prompt_toolkit_is_not_imported_by_models_or_catalog():
    code = (
        "import sys; "
        "import reviewer.launcher.models, reviewer.launcher.catalog; "
        "assert 'prompt_toolkit' not in sys.modules"
    )

    subprocess.run([sys.executable, "-c", code], check=True)


def test_app_imports_with_early_prompt_toolkit_exports():
    code = (
        "import prompt_toolkit.key_binding, prompt_toolkit.layout; "
        "del prompt_toolkit.key_binding.KeyPressEvent; "
        "del prompt_toolkit.layout.ScrollablePane; "
        "import reviewer.launcher.app"
    )

    subprocess.run([sys.executable, "-c", code], check=True)


def test_update_cancel_does_not_detect_installation_or_check_network():
    calls: list[str] = []

    def detector() -> InstallationInfo:
        calls.append("detect")
        raise AssertionError("startup не должен определять установку")

    def checker(info: InstallationInfo, *, timeout: int) -> VersionCheck:
        calls.append("check")
        raise AssertionError("startup не должен обращаться к PyPI")

    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\x1b")
        result = run_launcher(
            commands=(_spec("update"),),
            input=pipe,
            output=DummyOutput(),
            installation_detector=detector,
            version_checker=checker,
        )

    assert result == LauncherResult(None, 0)
    assert calls == []


def test_update_check_runs_once_and_does_not_return_upgrade_without_confirm():
    checked = threading.Event()
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv")
    calls: list[tuple[InstallationInfo, int]] = []

    def checker(installation: InstallationInfo, *, timeout: int) -> VersionCheck:
        calls.append((installation, timeout))
        checked.set()
        return VersionCheck(installation, "0.5.0", True)

    with create_pipe_input() as pipe:
        sender = _send_after(pipe, checked, b"\x03")
        pipe.send_bytes(b"\r\r\r")
        result = run_launcher(
            commands=(_spec("update"),),
            input=pipe,
            output=DummyOutput(),
            installation_detector=lambda: info,
            version_checker=checker,
        )
        sender.join()

    assert result == LauncherResult(None, 130)
    assert calls == [(info, 5)]


def test_update_check_renders_progress_while_executor_is_running():
    checker_started = threading.Event()
    release_checker = threading.Event()
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv")
    output = _TrackingOutput()

    def checker(installation: InstallationInfo, *, timeout: int) -> VersionCheck:
        checker_started.set()
        release_checker.wait(2)
        return VersionCheck(installation, "0.5.0", True)

    def sender(pipe) -> None:
        checker_started.wait(2)
        output.progress_rendered.wait(1)
        release_checker.set()
        pipe.send_bytes(b"\x03")

    with create_pipe_input() as pipe:
        thread = threading.Thread(target=sender, args=(pipe,))
        thread.start()
        pipe.send_bytes(b"\r\r")
        result = run_launcher(
            commands=(_spec("update"),),
            input=pipe,
            output=output,
            installation_detector=lambda: info,
            version_checker=checker,
        )
        thread.join()

    assert result == LauncherResult(None, 130)
    assert output.progress_rendered.is_set(), output.stream.getvalue()


def test_ctrl_c_returns_while_update_checker_is_still_blocked():
    checker_started = threading.Event()
    checker_completed = threading.Event()
    release_checker = threading.Event()
    launcher_finished = threading.Event()
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv")
    output = _TrackingOutput()
    results: list[LauncherResult] = []
    failures: list[BaseException] = []
    workers: list[threading.Thread] = []

    def checker(installation: InstallationInfo, *, timeout: int) -> VersionCheck:
        workers.append(threading.current_thread())
        checker_started.set()
        release_checker.wait()
        checker_completed.set()
        return VersionCheck(installation, "0.5.0", True)

    with create_pipe_input() as pipe:

        def launch() -> None:
            try:
                results.append(
                    run_launcher(
                        commands=(_spec("update"),),
                        input=pipe,
                        output=output,
                        installation_detector=lambda: info,
                        version_checker=checker,
                    )
                )
            except BaseException as error:
                failures.append(error)
            finally:
                launcher_finished.set()

        thread = threading.Thread(target=launch)
        thread.start()
        returned_before_release = False
        checker_did_complete = False
        rendered_after_close = ""
        try:
            pipe.send_bytes(b"\r\r")
            assert checker_started.wait(2)
            pipe.send_bytes(b"\x03")
            returned_before_release = launcher_finished.wait(2)
            rendered_after_close = output.stream.getvalue()
            assert not release_checker.is_set()
        finally:
            release_checker.set()
            checker_did_complete = checker_completed.wait(2)
            for worker in workers:
                worker.join(2)
            thread.join(2)

    assert returned_before_release
    assert checker_did_complete
    assert not thread.is_alive()
    assert len(workers) == 1
    assert workers[0].daemon
    assert not workers[0].is_alive()
    assert failures == []
    assert results == [LauncherResult(None, 130)]
    assert output.stream.getvalue() == rendered_after_close


def test_done_application_rejects_late_update_result():
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv")
    result = VersionCheck(info, "0.5.0", True)
    controller = LauncherController((_spec("update"),))
    controller.screen = Screen.UPDATE_RESULT
    ui = _LauncherUI(
        controller,
        installation_detector=lambda: info,
        version_checker=lambda installation, timeout: result,
    )
    token = object()
    ui.checking_update = True
    ui._active_update_token = token
    ui._visible_update_token = token
    application: Application[LauncherResult] = Application(
        input=DummyInput(),
        output=DummyOutput(),
    )
    loop = asyncio.new_event_loop()
    application.future = loop.create_future()
    application.future.set_result(LauncherResult(None, 130))
    try:
        ui._finish_update_check(token, application, result, None)
    finally:
        application.future = None
        loop.close()

    assert ui.version_check is None
    assert ui.update_error is None
    assert ui.checking_update is True
    assert ui._active_update_token is token
    assert ui._visible_update_token is token


def test_late_update_completion_does_not_take_over_another_command():
    release_checker = threading.Event()
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv")
    output = _TrackingOutput()
    calls = 0

    def checker(installation: InstallationInfo, *, timeout: int) -> VersionCheck:
        nonlocal calls
        calls += 1
        release_checker.wait(4)
        return VersionCheck(installation, "0.5.0", True)

    with create_pipe_input() as pipe:
        timers = [
            threading.Timer(0.5, lambda: pipe.send_bytes(b"\x1b")),
            threading.Timer(1.2, lambda: pipe.send_bytes(b"\x1b")),
            threading.Timer(1.9, lambda: pipe.send_bytes(b"\x1b[B\r")),
            threading.Timer(2.5, release_checker.set),
            threading.Timer(3, lambda: pipe.send_bytes(b"\r\r")),
        ]
        watchdog = threading.Timer(4.5, lambda: pipe.send_bytes(b"\x03"))
        for timer in timers:
            timer.start()
        watchdog.start()
        pipe.send_bytes(b"\r\r")
        result = run_launcher(
            commands=(_spec("update"), _spec("status")),
            input=pipe,
            output=output,
            installation_detector=lambda: info,
            version_checker=checker,
        )
        watchdog.cancel()
        watchdog.join()
        for timer in timers:
            timer.join()

    assert result == LauncherResult(("status",), 0), output.stream.getvalue()
    assert calls == 1


def test_completed_update_result_reopens_without_second_network_call():
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv")
    output = _TrackingOutput()
    calls = 0

    def checker(installation: InstallationInfo, *, timeout: int) -> VersionCheck:
        nonlocal calls
        calls += 1
        return VersionCheck(installation, "0.5.0", True)

    with create_pipe_input() as pipe:
        back = threading.Timer(0.5, lambda: pipe.send_bytes(b"\x1b"))
        reopen = threading.Timer(1.2, lambda: pipe.send_bytes(b"\r\r"))
        watchdog = threading.Timer(3, lambda: pipe.send_bytes(b"\x03"))
        back.start()
        reopen.start()
        watchdog.start()
        pipe.send_bytes(b"\r\r")
        result = run_launcher(
            commands=(_spec("update"),),
            input=pipe,
            output=output,
            installation_detector=lambda: info,
            version_checker=checker,
        )
        watchdog.cancel()
        watchdog.join()
        back.join()
        reopen.join()

    assert result == LauncherResult(("update",), 0), output.stream.getvalue()
    assert calls == 1
    assert output.update_result_rendered.is_set(), output.stream.getvalue()


def test_update_confirm_returns_existing_click_argv_for_uv_tool():
    checked = threading.Event()
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv")

    def checker(installation: InstallationInfo, *, timeout: int) -> VersionCheck:
        assert timeout == 5
        checked.set()
        return VersionCheck(installation, "0.5.0", True)

    with create_pipe_input() as pipe:
        sender = _send_after(pipe, checked, b"\r")
        pipe.send_bytes(b"\r\r")
        result = run_launcher(
            commands=(_spec("update"),),
            input=pipe,
            output=DummyOutput(),
            installation_detector=lambda: info,
            version_checker=checker,
        )
        sender.join()

    assert result == LauncherResult(("update",), 0)


def test_uvx_update_result_only_shows_instructions_without_upgrade_argv():
    checked = threading.Event()
    info = InstallationInfo(InstallMode.UVX, "0.4.0", "/usr/bin/uv")

    def checker(installation: InstallationInfo, *, timeout: int) -> VersionCheck:
        checked.set()
        return VersionCheck(installation, "0.5.0", True)

    with create_pipe_input() as pipe:
        sender = _send_after(pipe, checked, b"\r\x03")
        pipe.send_bytes(b"\r\r")
        result = run_launcher(
            commands=(_spec("update"),),
            input=pipe,
            output=DummyOutput(),
            installation_detector=lambda: info,
            version_checker=checker,
        )
        sender.join()

    assert result == LauncherResult(None, 130)
