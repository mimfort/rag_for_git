from __future__ import annotations

import subprocess
import sys
import threading
import time
from io import StringIO

from reviewer.entrypoints.cli import cli
from reviewer.launcher.app import run_launcher
from reviewer.launcher.catalog import build_catalog
from reviewer.launcher.models import CommandSpec, LauncherResult
from reviewer.versioning import InstallMode, InstallationInfo, VersionCheck

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.plain_text import PlainTextOutput


def _spec(name: str) -> CommandSpec:
    return next(item for item in build_catalog(cli) if item.path == (name,))


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
        super().__init__(self.stream)

    def write(self, data: str) -> None:
        super().write(data)
        if "Проверяем способ установки" in self.stream.getvalue():
            self.progress_rendered.set()


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
