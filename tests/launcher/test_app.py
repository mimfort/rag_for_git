from __future__ import annotations

import subprocess
import sys
import threading
import time
from io import StringIO

import click

from reviewer.entrypoints.cli import cli
from reviewer.launcher.app import run_launcher
from reviewer.launcher.catalog import build_catalog
from reviewer.launcher.models import CommandSpec, LauncherResult, ParameterSpec, ParamSection
from reviewer.versioning import InstallMode, InstallationInfo, VersionCheck

from prompt_toolkit.input import create_pipe_input
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
            "Ошибка repo: Обязательное поле" in rendered
            and not self.required_error_rendered.is_set()
        ):
            self.required_error_rendered.set()
        if "Ошибка port:" in rendered:
            self.type_error_rendered.set()
        if "Доступна новая версия: 0.4.0 → 0.5.0" in rendered:
            self.update_result_rendered.set()
        if (
            self.required_error_rendered.is_set()
            and "owner/repo" in self._frame
            and "Расширенные параметры:" in self._frame
            and "Ошибка repo:" not in self._frame
        ):
            self.required_error_removed.set()

    def erase_down(self) -> None:
        self._frame = ""
        super().erase_down()


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


def test_prompt_toolkit_is_not_imported_by_models_or_catalog():
    code = (
        "import sys; "
        "import reviewer.launcher.models, reviewer.launcher.catalog; "
        "assert 'prompt_toolkit' not in sys.modules"
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
