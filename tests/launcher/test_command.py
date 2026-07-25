from __future__ import annotations

import subprocess

import click
import pytest
from click.testing import CliRunner

from reviewer.entrypoints.cli import cli
from reviewer.launcher.catalog import build_catalog
from reviewer.launcher.command import prepare_command
from reviewer.launcher.models import (
    CommandSpec,
    Effect,
    ParameterSpec,
    ParamSection,
)


def _command(name: str) -> CommandSpec:
    return next(item for item in build_catalog(cli) if item.path == (name,))


def _secret_command() -> CommandSpec:
    source = click.Option(["--token"])
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
        path=("deploy",),
        command=click.Command("deploy", params=[source]),
        summary="Развернуть",
        details="Тестовая команда",
        effects=(Effect.WRITE,),
        scenarios=(),
        keywords=(),
        params=(parameter,),
    )


def _parameter(parameter: click.Parameter) -> ParameterSpec:
    return ParameterSpec(
        source=parameter,
        name=parameter.name,
        kind="argument" if isinstance(parameter, click.Argument) else "option",
        option_strings=tuple(parameter.opts),
        secondary_strings=tuple(parameter.secondary_opts),
        required=parameter.required,
        nargs=parameter.nargs,
        multiple=parameter.multiple,
        count=getattr(parameter, "count", False),
        is_flag=getattr(parameter, "is_flag", False),
        default=parameter.default,
        choices=(),
        section=ParamSection.BASIC,
        sensitive=False,
    )


def _test_command(*parameters: click.Parameter) -> CommandSpec:
    command = click.Command("run", params=list(parameters))
    return CommandSpec(
        path=("run",),
        command=command,
        summary="Запустить",
        details="Тестовая команда",
        effects=(),
        scenarios=(),
        keywords=(),
        params=tuple(_parameter(parameter) for parameter in parameters),
    )


def test_prepare_status_omits_unchanged_defaults_and_emits_changed_flags():
    """Неизменённые опции не попадают в argv, а флаги попадают."""
    status = _command("status")

    prepared = prepare_command(
        status,
        values={"path": ".", "repo_tag": "a/x", "as_json": True},
        changed={"repo_tag", "as_json"},
        platform_name="Linux",
    )

    assert prepared.argv == ("status", "--repo", "a/x", "--json")
    assert prepared.preview == "reviewer status --repo a/x --json"


def test_sensitive_value_is_real_in_argv_but_masked_everywhere_else():
    """Секрет нужен процессу, но не должен попадать в preview или repr."""
    spec = _secret_command()

    prepared = prepare_command(
        spec,
        values={"token": "secret-123"},
        changed={"token"},
        platform_name="Linux",
    )

    assert prepared.argv == ("deploy", "--token", "secret-123")
    assert "secret-123" not in prepared.preview
    assert "secret-123" not in repr(prepared)
    assert "••••••" in prepared.preview


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("--no-skills", ("run", "--", "--no-skills")),
        ("--", ("run", "--", "--")),
    ],
)
def test_prepare_command_protects_leading_dash_positional_for_click(value, expected):
    """Разделитель сохраняет похожие на опции значения обычными аргументами Click."""
    argument = click.Argument(["query"])
    received: list[str] = []
    spec = _test_command(argument)
    spec.command.callback = lambda query: received.append(query)

    prepared = prepare_command(
        spec,
        {"query": value},
        {"query"},
        platform_name="Linux",
    )
    result = CliRunner().invoke(spec.command, list(prepared.argv[1:]))

    assert prepared.argv == expected
    assert prepared.preview == f"reviewer run -- {value}"
    assert result.exit_code == 0, result.output
    assert received == [value]


def test_prepare_command_places_options_before_protected_positional_block():
    """Опции идут перед единым защищённым блоком позиционных аргументов."""
    argument = click.Argument(["query"])
    option = click.Option(["--repo"])
    received: list[tuple[str, str]] = []
    spec = _test_command(argument, option)
    spec.command.callback = lambda query, repo: received.append((query, repo))

    prepared = prepare_command(
        spec,
        {"query": "--no-skills", "repo": "owner/name"},
        {"query", "repo"},
        platform_name="Linux",
    )
    result = CliRunner().invoke(spec.command, list(prepared.argv[1:]))

    assert prepared.argv == ("run", "--repo", "owner/name", "--", "--no-skills")
    assert prepared.preview == "reviewer run --repo owner/name -- --no-skills"
    assert result.exit_code == 0, result.output
    assert received == [("--no-skills", "owner/name")]


@pytest.mark.parametrize(
    ("argument", "value", "expected_value"),
    [
        (
            click.Argument(["parts"], nargs=2),
            ("--left", "right"),
            ("--left", "right"),
        ),
        (
            click.Argument(["items"], nargs=-1),
            ("first", "--second"),
            ("first", "--second"),
        ),
    ],
)
def test_prepare_command_protects_fixed_and_variadic_positionals(argument, value, expected_value):
    """Один разделитель защищает все токены fixed и variadic аргументов."""
    received: list[tuple[str, ...]] = []
    spec = _test_command(argument)
    spec.command.callback = lambda **values: received.append(values[argument.name])

    prepared = prepare_command(
        spec,
        {argument.name: value},
        {argument.name},
        platform_name="Linux",
    )
    result = CliRunner().invoke(spec.command, list(prepared.argv[1:]))

    assert prepared.argv == ("run", "--", *value)
    assert prepared.preview == f"reviewer run -- {' '.join(value)}"
    assert result.exit_code == 0, result.output
    assert received == [expected_value]


@pytest.mark.parametrize(
    ("parameter", "value", "changed", "expected"),
    [
        (click.Argument(["repo"]), "/tmp/repo", {"repo"}, ("run", "/tmp/repo")),
        (click.Argument(["path"], required=False), None, set(), ("run",)),
        (click.Option(["--cache/--no-cache"]), False, {"cache"}, ("run", "--no-cache")),
        (
            click.Option(["--label"], multiple=True),
            ("one", "two"),
            {"label"},
            ("run", "--label", "one", "--label", "two"),
        ),
        (click.Option(["-v"], count=True), 2, {"v"}, ("run", "-v", "-v")),
        (
            click.Option(["--pair"], nargs=2),
            ("left", "right"),
            {"pair"},
            ("run", "--pair", "left", "right"),
        ),
    ],
)
def test_prepare_command_serializes_click_parameter_shapes(parameter, value, changed, expected):
    """Построитель argv сохраняет семантику основных форм параметров Click."""
    spec = _test_command(parameter)

    prepared = prepare_command(spec, {parameter.name: value}, changed, platform_name="Linux")

    assert prepared.argv == expected


def test_windows_preview_uses_windows_quoting_after_masking_secret():
    """Windows preview экранируется list2cmdline и маскирует секрет до форматирования."""
    prepared = prepare_command(
        _secret_command(),
        values={"token": "secret with spaces"},
        changed={"token"},
        platform_name="Windows",
    )

    assert prepared.preview == subprocess.list2cmdline(("reviewer", "deploy", "--token", "••••••"))
