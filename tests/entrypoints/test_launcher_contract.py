"""Регрессионные контракты публичного launcher."""
from __future__ import annotations

import importlib.metadata
import json
import subprocess
import sys
from unittest.mock import MagicMock, Mock

import pytest
from click.testing import CliRunner

from reviewer.entrypoints import launcher
import reviewer.entrypoints.cli as cli_mod
from reviewer.entrypoints.cli import cli

pytest_plugins = ("tests.services.test_status",)

_NON_TTY_COLUMNS = 40
_NON_TTY_WIDTH = 50


def test_installed_reviewer_entry_point_loads_launcher():
    """Публичная команда reviewer загружает безопасный launcher."""
    scripts = {
        item.name: item
        for item in importlib.metadata.entry_points(group="console_scripts")
        if item.name in {"reviewer", "reviewer-mcp"}
    }

    assert scripts["reviewer"].value == "reviewer.entrypoints.launcher:main"
    assert scripts["reviewer"].load() is launcher.main
    assert scripts["reviewer-mcp"].value == "reviewer.entrypoints.mcp_server:main"


@pytest.mark.parametrize(
    "argv",
    [
        ("--help",),
        ("check",),
        ("status", ".", "--repo", "a/x", "--json"),
        ("init", "--yes", "--dry-run"),
        ("install", "codex", "--dry-run"),
        ("does-not-exist",),
    ],
)
def test_direct_contract_never_enters_tui(monkeypatch, argv):
    """Явные аргументы всегда передаются Click без запуска TUI."""
    tui = Mock(side_effect=AssertionError("TUI вызван на direct route"))
    cli_call = Mock()
    monkeypatch.setattr(launcher, "_run_tui", tui)
    monkeypatch.setattr(launcher, "_run_cli", cli_call)

    launcher.main(argv)

    cli_call.assert_called_once_with(argv)
    tui.assert_not_called()


def _invoke_launcher(argv: tuple[str, ...], capsys) -> tuple[int, str, str]:
    try:
        launcher.main(argv)
    except SystemExit as error:
        code = int(error.code or 0)
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("argv", [("--help",), ("does-not-exist",)])
def test_launcher_output_matches_direct_click(monkeypatch, argv, capsys):
    """Прямой маршрут сохраняет код и байты вывода Click."""
    monkeypatch.setenv("COLUMNS", str(_NON_TTY_COLUMNS))
    direct = CliRunner().invoke(
        cli,
        list(argv),
        prog_name="reviewer",
        terminal_width=_NON_TTY_WIDTH,
    )
    actual = _invoke_launcher(argv, capsys)

    assert actual == (direct.exit_code, direct.stdout, direct.stderr)


def test_launcher_status_json_is_clean(monkeypatch, capsys, status_report):
    """JSON статуса не содержит управляющих последовательностей launcher."""
    monkeypatch.setattr(
        cli_mod, "build_status_report", lambda *args, **kwargs: status_report
    )
    monkeypatch.setattr(cli_mod, "ChunkStore", MagicMock())
    monkeypatch.setattr(cli_mod, "GraphStore", MagicMock())
    monkeypatch.setattr(cli_mod, "SummaryStore", MagicMock())

    code, stdout, stderr = _invoke_launcher(
        ("status", ".", "--repo", "a/x", "--json"),
        capsys,
    )

    assert code == 0
    assert stderr == ""
    assert json.loads(stdout)["repo"] == "a/x"
    assert "\x1b" not in stdout


def test_non_tty_no_args_delegates_existing_click(monkeypatch):
    """Пустой неинтерактивный вызов остаётся исходным Click-маршрутом."""
    monkeypatch.setattr(launcher, "should_use_tui", lambda *args, **kwargs: False)
    direct = Mock()
    monkeypatch.setattr(launcher, "_run_cli", direct)

    launcher.main([])

    direct.assert_called_once_with(())


def test_mcp_import_does_not_load_launcher_or_prompt_toolkit():
    """Импорт MCP не должен тянуть интерактивные зависимости."""
    code = (
        "import sys; "
        "import reviewer.entrypoints.mcp_server; "
        "assert 'reviewer.entrypoints.launcher' not in sys.modules; "
        "assert 'prompt_toolkit' not in sys.modules"
    )

    subprocess.run([sys.executable, "-c", code], check=True)
