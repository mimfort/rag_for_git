"""Проверки маршрутизации глобального launcher."""

from __future__ import annotations

import subprocess
import sys

import pytest

from reviewer.entrypoints import launcher
from reviewer.entrypoints.launcher import should_use_tui
from reviewer.launcher.models import LauncherResult


class _Stream:
    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


class _BrokenStream:
    def isatty(self) -> bool:
        raise OSError("Поток недоступен")


@pytest.mark.parametrize(
    ("argv", "stdin_tty", "stdout_tty", "env", "expected"),
    [
        (["--help"], True, True, {}, False),
        ([], False, True, {}, False),
        ([], True, False, {}, False),
        ([], True, True, {"TERM": "dumb"}, False),
        ([], True, True, {"CI": "true"}, False),
        ([], True, True, {"GITHUB_ACTIONS": "true"}, False),
        ([], True, True, {"CI": "false"}, False),
        ([], True, True, {}, True),
    ],
)
def test_should_use_tui_matrix(argv, stdin_tty, stdout_tty, env, expected):
    """Интерактивный режим включается только в подходящем терминале."""
    assert (
        should_use_tui(
            argv,
            stdin=_Stream(stdin_tty),
            stdout=_Stream(stdout_tty),
            environ=env,
        )
        is expected
    )


@pytest.mark.parametrize(
    "marker",
    [
        "CI",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "TF_BUILD",
        "BUILDKITE",
        "CIRCLECI",
        "JENKINS_URL",
        "TEAMCITY_VERSION",
    ],
)
def test_empty_ci_marker_routes_direct_without_loading_prompt_toolkit(marker):
    """Даже пустой CI-маркер не должен загружать или запускать TUI."""
    code = """
import sys
from reviewer.entrypoints import launcher

class TTY:
    def isatty(self):
        return True
    def flush(self):
        pass

launcher.sys.stdin = TTY()
launcher.sys.stdout = TTY()
launcher.os.environ = {"TERM": "xterm-256color", sys.argv[1]: ""}
calls = []
launcher._run_cli = lambda argv: calls.append(tuple(argv))
launcher._run_tui = lambda: (_ for _ in ()).throw(AssertionError("TUI вызван в CI"))
launcher.main([])
assert calls == [()]
assert "prompt_toolkit" not in sys.modules
"""

    subprocess.run([sys.executable, "-c", code, marker], check=True)


@pytest.mark.parametrize(
    ("stdin", "stdout"),
    [(_BrokenStream(), _Stream(True)), (_Stream(True), _BrokenStream())],
)
def test_should_use_tui_returns_false_when_isatty_raises(stdin, stdout):
    """Ошибка проверки терминала должна закрывать интерактивный маршрут."""
    assert should_use_tui([], stdin=stdin, stdout=stdout, environ={}) is False


def test_direct_route_forwards_original_argv_without_loading_tui(monkeypatch):
    """Явная CLI-команда не должна загружать интерактивную границу."""
    calls = []
    monkeypatch.setattr(launcher, "_run_cli", lambda argv: calls.append(tuple(argv)), raising=False)
    monkeypatch.setattr(
        launcher,
        "_run_tui",
        lambda: pytest.fail("TUI не должен загружаться"),
        raising=False,
    )

    launcher.main(["status", "--json"])

    assert calls == [("status", "--json")]


def test_interactive_confirm_runs_existing_cli(monkeypatch):
    """Подтверждённая в TUI команда передаётся существующему CLI."""
    calls = []
    monkeypatch.setattr(launcher, "should_use_tui", lambda *a, **k: True)
    monkeypatch.setattr(
        launcher,
        "_run_tui",
        lambda: LauncherResult(("check", "--board-project", "yougile=PRI"), 0),
        raising=False,
    )
    monkeypatch.setattr(launcher, "_run_cli", lambda argv: calls.append(tuple(argv)), raising=False)

    launcher.main([])

    assert calls == [("check", "--board-project", "yougile=PRI")]


def test_interactive_cancel_uses_result_exit_code(monkeypatch):
    """Отмена launcher завершает процесс кодом результата."""
    monkeypatch.setattr(launcher, "should_use_tui", lambda *a, **k: True)
    monkeypatch.setattr(
        launcher,
        "_run_tui",
        lambda: LauncherResult(None, 0),
        raising=False,
    )
    monkeypatch.setattr(
        launcher,
        "_run_cli",
        lambda argv: pytest.fail("Click не должен вызываться"),
        raising=False,
    )

    with pytest.raises(SystemExit, match="0") as exc_info:
        launcher.main([])

    assert exc_info.value.code == 0


def test_keyboard_interrupt_exits_with_130(monkeypatch):
    """Прерывание TUI получает стандартный код завершения 130."""
    monkeypatch.setattr(launcher, "should_use_tui", lambda *a, **k: True)
    monkeypatch.setattr(
        launcher,
        "_run_tui",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt),
        raising=False,
    )

    with pytest.raises(SystemExit, match="130") as exc_info:
        launcher.main([])

    assert exc_info.value.code == 130


def test_tui_exception_prints_help_and_does_not_invoke_click(monkeypatch, capsys):
    """Ошибка TUI завершается русской подсказкой без запуска Click."""
    monkeypatch.setattr(launcher, "should_use_tui", lambda *a, **k: True)
    monkeypatch.setattr(
        launcher,
        "_run_tui",
        lambda: (_ for _ in ()).throw(RuntimeError("сбой")),
        raising=False,
    )
    monkeypatch.setattr(
        launcher,
        "_run_cli",
        lambda argv: pytest.fail("Click не должен вызываться"),
        raising=False,
    )

    with pytest.raises(SystemExit, match="1") as exc_info:
        launcher.main([])

    assert exc_info.value.code == 1
    assert capsys.readouterr().err == (
        "reviewer: интерактивный launcher недоступен; используйте reviewer --help\n"
    )
